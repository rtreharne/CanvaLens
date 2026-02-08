from functools import wraps
import re
import secrets
import json
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login as auth_login
from django.db import transaction
from django.contrib.auth.models import User
from django.core import signing
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime, parse_date
from datetime import datetime, time, timedelta
from django.views.decorators.http import require_GET, require_POST

from .canvas_client import CanvasClient, CanvasClientError
from .models import (
    CanvasAssignment,
    CanvasAssignmentModerationReport,
    CanvasModerationAssignmentPreference,
    CanvasModerationSubmissionReview,
    CanvasSubAccount,
    CanvasCourse,
    CanvasCredential,
    CanvasStaffMarkingReport,
    CanvasSubmissionReport,
)
from .tasks import (
    _build_checked_submissions,
    generate_assignment_moderation_report,
    generate_staff_marking_report,
    generate_submissions_report,
    sync_canvas_for_user,
)

EMBED_AUTH_SALT = "canvaslens.embed_auth_handoff"
EMBED_AUTH_MAX_AGE_SECONDS = 300


def app_user_required(view_func):
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect("login")
        return view_func(request, *args, **kwargs)

    return _wrapped


def owner_account_required(view_func):
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect("login")
        if hasattr(request.user, "canvas_subaccount_profile"):
            messages.error(request, "Sub-accounts cannot access the admin dashboard.")
            return redirect("canvas_assignments")
        return view_func(request, *args, **kwargs)

    return _wrapped


def _effective_canvas_user(user):
    profile = getattr(user, "canvas_subaccount_profile", None)
    if profile and profile.owner_id:
        return profile.owner
    return user


def _related_canvas_user_ids(user):
    related_user_ids = {user.id}
    profile = getattr(user, "canvas_subaccount_profile", None)
    if profile and profile.owner_id:
        related_user_ids.add(profile.owner_id)
        owner_sub_ids = CanvasSubAccount.objects.filter(owner_id=profile.owner_id).values_list("user_id", flat=True)
        related_user_ids.update(owner_sub_ids)
    else:
        sub_ids = CanvasSubAccount.objects.filter(owner_id=user.id).values_list("user_id", flat=True)
        related_user_ids.update(sub_ids)
    return related_user_ids


def _related_sync_in_progress(user):
    related_user_ids = _related_canvas_user_ids(user)
    return CanvasCredential.objects.filter(
        user_id__in=related_user_ids,
        sync_status__in=["queued", "running"],
    ).exists()


def _is_subaccount_user(user):
    return bool(getattr(user, "canvas_subaccount_profile", None))


def _generate_memorable_password():
    words = [
        "amber", "anchor", "apricot", "atlas", "bamboo", "beacon", "berry", "blossom",
        "canyon", "cedar", "comet", "coral", "delta", "ember", "falcon", "forest",
        "glacier", "harbor", "hazel", "island", "jasmine", "lagoon", "lantern", "maple",
        "meadow", "meteor", "midnight", "nebula", "oasis", "opal", "orchid", "pepper",
        "planet", "prairie", "quartz", "raven", "river", "saffron", "sierra", "silver",
        "spruce", "summit", "sunset", "thunder", "timber", "tulip", "valley", "violet",
        "willow", "zephyr",
    ]
    selected = [secrets.choice(words) for _ in range(4)]
    number_part = str(secrets.randbelow(90) + 10)
    special_part = secrets.choice("!@#$%^&*?")
    return "-".join(selected) + number_part + special_part


def _admin_or_assignments_redirect(request):
    if _is_subaccount_user(request.user):
        return redirect("canvas_assignments")
    return redirect("admin_dashboard")


def _parse_filter_dt(value):
    raw = (value or "").strip()
    if not raw:
        return None
    dt = parse_datetime(raw)
    if dt:
        if timezone.is_naive(dt):
            dt = timezone.make_aware(dt, timezone.get_default_timezone())
        return dt
    d = parse_date(raw)
    if not d:
        return None
    return timezone.make_aware(datetime.combine(d, time.min), timezone.get_default_timezone())


def _load_available_accounts(client):
    accounts = client.list_manageable_accounts()
    if accounts:
        return accounts
    return client.list_accounts()


def _default_sync_start(now=None):
    now = now or timezone.now()
    tz = timezone.get_default_timezone()
    sept_this_year = timezone.make_aware(datetime(now.year, 9, 1, 0, 0, 0), tz)
    if now < sept_this_year:
        return timezone.make_aware(datetime(now.year - 1, 9, 1, 0, 0, 0), tz)
    return sept_this_year


def _as_datetime_local_value(dt):
    if not dt:
        return ""
    local_dt = timezone.localtime(dt)
    return local_dt.strftime("%Y-%m-%dT%H:%M")


def _course_name_query(value):
    raw = (value or "").strip()
    if not raw:
        return Q()

    combined_query = None
    for or_part in re.split(r"\bOR\b", raw, flags=re.IGNORECASE):
        and_terms = [term.strip() for term in re.split(r"\bAND\b", or_part, flags=re.IGNORECASE) if term.strip()]
        if not and_terms:
            continue

        part_query = Q()
        for term in and_terms:
            part_query &= Q(course__name__icontains=term) | Q(course__course_code__icontains=term)

        combined_query = part_query if combined_query is None else (combined_query | part_query)

    return combined_query if combined_query is not None else Q()


def _purge_expired_submission_reports(user):
    cutoff = timezone.now() - timedelta(hours=1)
    CanvasSubmissionReport.objects.filter(user=user, created_at__lt=cutoff).exclude(
        status__in=["pending", "running"]
    ).delete()
    CanvasStaffMarkingReport.objects.filter(user=user, created_at__lt=cutoff).exclude(
        status__in=["pending", "running"]
    ).delete()


def _is_ajax_request(request):
    return request.headers.get("x-requested-with") == "XMLHttpRequest"


def _safe_next_path(value, fallback="/"):
    next_path = (value or "").strip()
    if not next_path.startswith("/") or next_path.startswith("//"):
        return fallback
    return next_path


def _reports_for_user(user):
    submission_reports = list(
        CanvasSubmissionReport.objects.filter(user=user).order_by("-created_at")[:20]
    )
    staff_marking_reports = list(
        CanvasStaffMarkingReport.objects.filter(user=user).order_by("-created_at")[:20]
    )
    reports = [_serialize_submission_report_for_table(report) for report in submission_reports]
    reports.extend(_serialize_staff_marking_report_for_table(report) for report in staff_marking_reports)
    reports.sort(key=lambda row: row["created_at"], reverse=True)
    reports = reports[:20]
    active_report = next((row for row in reports if row["status"] in {"pending", "running"}), None)
    return reports, active_report


def _serialize_submission_report_for_table(report):
    return {
        "id": report.id,
        "kind": "submissions",
        "kind_label": "Submissions",
        "status": report.status,
        "created_at": report.created_at,
        "completed_at": report.completed_at,
        "row_count": report.row_count,
        "total_assignments": int(report.total_assignments or 0),
        "processed_assignments": int(report.processed_assignments or 0),
        "current_assignment_name": report.current_assignment_name or "",
        "cancel_requested": bool(report.cancel_requested),
        "download_url": f"/canvas/reports/{report.id}/download/",
        "cancel_url": f"/canvas/reports/{report.id}/cancel/",
        "delete_url": f"/canvas/reports/{report.id}/delete/",
    }


def _serialize_staff_marking_report_for_table(report):
    return {
        "id": report.id,
        "kind": "staff_marking",
        "kind_label": "Staff marking",
        "status": report.status,
        "created_at": report.created_at,
        "completed_at": report.completed_at,
        "row_count": report.row_count,
        "total_assignments": int(report.total_assignments or 0),
        "processed_assignments": int(report.processed_assignments or 0),
        "current_assignment_name": report.current_assignment_name or "",
        "cancel_requested": bool(report.cancel_requested),
        "download_url": f"/canvas/staff-marking-reports/{report.id}/download/",
        "cancel_url": f"/canvas/staff-marking-reports/{report.id}/cancel/",
        "delete_url": f"/canvas/staff-marking-reports/{report.id}/delete/",
    }


def _active_report_objects_for_user(user):
    active_submission = CanvasSubmissionReport.objects.filter(
        user=user, status__in=["pending", "running"]
    ).order_by("-created_at").first()
    active_staff_marking = CanvasStaffMarkingReport.objects.filter(
        user=user, status__in=["pending", "running"]
    ).order_by("-created_at").first()
    candidates = [item for item in [active_submission, active_staff_marking] if item is not None]
    if not candidates:
        return None, None
    active_obj = max(candidates, key=lambda obj: obj.created_at)
    if isinstance(active_obj, CanvasSubmissionReport):
        return "submissions", active_obj
    return "staff_marking", active_obj


def _rubric_criterion_names_from_raw(raw_data):
    names = []
    rubric = (raw_data or {}).get("rubric") or []
    for criterion in rubric:
        name = (
            criterion.get("description")
            or criterion.get("long_description")
            or criterion.get("criterion")
            or ""
        ).strip()
        if name:
            names.append(name)
    return names


def _normalize_rubric_criterion_name(value):
    return " ".join((value or "").strip().split()).casefold()


def _assignment_has_rubric_criterion(assignment, criterion_name):
    target = _normalize_rubric_criterion_name(criterion_name)
    if not target:
        return True
    for name in _rubric_criterion_names_from_raw(assignment.raw_data):
        if _normalize_rubric_criterion_name(name) == target:
            return True
    return False


def _build_canvas_assignments_context(request):
    canvas_user = _effective_canvas_user(request.user)
    course_id = (request.GET.get("course") or "").strip()
    course_name = (request.GET.get("course_name") or "").strip()
    assignment_type = (request.GET.get("assignment_type") or "").strip()
    rubric_criterion = (request.GET.get("rubric_criterion") or "").strip()
    assignment_name = (request.GET.get("assignment_name") or "").strip()
    enrolled_filter = (request.GET.get("enrolled") or "all").strip().lower()
    needs_grading_filter = (request.GET.get("needs_grading") or "all").strip().lower()

    date_from = _parse_filter_dt(request.GET.get("date_from"))
    date_to = _parse_filter_dt(request.GET.get("date_to"))

    courses = CanvasCourse.objects.filter(user=canvas_user, is_active=True).order_by("name")

    assignments = CanvasAssignment.objects.select_related("course").filter(
        course__user=canvas_user,
        course__is_active=True,
        is_active=True,
        published=True,
    )

    if course_id:
        assignments = assignments.filter(course_id=course_id)
    if course_name:
        assignments = assignments.filter(_course_name_query(course_name))
    if assignment_type:
        assignments = assignments.filter(submission_types__contains=[assignment_type])
    if enrolled_filter == "enrolled":
        assignments = assignments.filter(course__is_enrolled=True)
    elif enrolled_filter == "not_enrolled":
        assignments = assignments.filter(course__is_enrolled=False)
    if needs_grading_filter == "nonzero":
        assignments = assignments.filter(raw_data__needs_grading_count__gt=0)
    if assignment_name:
        assignments = assignments.filter(name__icontains=assignment_name)
    if date_from:
        assignments = assignments.filter(
            Q(unlock_at__gte=date_from) | Q(close_at__gte=date_from) | Q(due_at__gte=date_from)
        )
    if date_to:
        assignments = assignments.filter(
            Q(unlock_at__lte=date_to) | Q(close_at__lte=date_to) | Q(due_at__lte=date_to)
        )

    source_assignments = CanvasAssignment.objects.filter(
        course__user=canvas_user,
        course__is_active=True,
        is_active=True,
        published=True,
    )

    assignment_type_values = set()
    rubric_criteria_map = {}
    for raw_data, types in source_assignments.values_list("raw_data", "submission_types"):
        for item in types or []:
            if item:
                assignment_type_values.add(item)
        for name in _rubric_criterion_names_from_raw(raw_data):
            key = _normalize_rubric_criterion_name(name)
            if not key:
                continue
            rubric_criteria_map.setdefault(key, name)

    ordered_assignments = assignments.order_by("due_at", "name")
    if rubric_criterion:
        filtered_assignments = [
            a for a in ordered_assignments if _assignment_has_rubric_criterion(a, rubric_criterion)
        ]
        assignments_result = filtered_assignments[:500]
    else:
        assignments_result = ordered_assignments[:500]

    selected = {
        "course": course_id,
        "course_name": course_name,
        "assignment_type": assignment_type,
        "rubric_criterion": rubric_criterion,
        "enrolled": enrolled_filter,
        "needs_grading": needs_grading_filter,
        "assignment_name": assignment_name,
        "date_from": request.GET.get("date_from", ""),
        "date_to": request.GET.get("date_to", ""),
    }

    has_active_filters = bool(
        selected["course"]
        or selected["course_name"]
        or selected["assignment_type"]
        or selected["rubric_criterion"]
        or selected["assignment_name"]
        or selected["date_from"]
        or selected["date_to"]
        or selected["enrolled"] != "all"
        or selected["needs_grading"] != "all"
    )

    return {
        "courses": courses,
        "assignments": assignments_result,
        "assignment_types": sorted(assignment_type_values),
        "rubric_criteria": sorted(rubric_criteria_map.values(), key=str.casefold),
        "selected": selected,
        "has_active_filters": has_active_filters,
    }


@require_GET
@app_user_required
def index(request):
    return redirect("canvas_assignments")


@require_GET
def embed_auth_start(request):
    next_path = _safe_next_path(request.GET.get("next"), "/")
    if request.user.is_authenticated:
        return redirect(next_path)

    finish_url = f"/embed/auth/finish/?{urlencode({'next': next_path})}"
    login_url = f"/login/?{urlencode({'next': finish_url})}"
    return render(
        request,
        "directory/embed_auth_start.html",
        {
            "next_path": next_path,
            "login_url": login_url,
        },
    )


@require_GET
@app_user_required
def embed_auth_finish(request):
    next_path = _safe_next_path(request.GET.get("next"), "/")
    token = signing.dumps(
        {
            "user_id": request.user.id,
            "next_path": next_path,
            "nonce": secrets.token_urlsafe(16),
        },
        salt=EMBED_AUTH_SALT,
    )
    consume_url = f"/embed/auth/consume/?{urlencode({'token': token})}"
    return render(
        request,
        "directory/embed_auth_finish.html",
        {
            "consume_url": consume_url,
            "next_path": next_path,
        },
    )


@require_GET
def embed_auth_consume(request):
    token = (request.GET.get("token") or "").strip()
    if not token:
        return redirect("/embed/auth/start/?next=%2F")

    try:
        payload = signing.loads(
            token,
            salt=EMBED_AUTH_SALT,
            max_age=EMBED_AUTH_MAX_AGE_SECONDS,
        )
    except signing.SignatureExpired:
        return redirect("/embed/auth/start/?next=%2F")
    except signing.BadSignature:
        return redirect("/embed/auth/start/?next=%2F")

    user_id = payload.get("user_id")
    next_path = _safe_next_path(payload.get("next_path"), "/")
    if not user_id:
        return redirect("/embed/auth/start/?next=%2F")

    user = User.objects.filter(id=user_id, is_active=True).first()
    if not user:
        return redirect("/embed/auth/start/?next=%2F")

    auth_login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    return redirect(next_path)


@require_GET
@owner_account_required
def admin_dashboard(request):
    credential, _ = CanvasCredential.objects.get_or_create(user=request.user)
    manageable_accounts = []
    accounts_error = ""
    if credential.token:
        try:
            client = CanvasClient(settings.CANVAS_URL, credential.token)
            manageable_accounts = _load_available_accounts(client)
        except CanvasClientError as exc:
            accounts_error = str(exc)
    courses_count = CanvasCourse.objects.filter(user=request.user, is_active=True).count()
    assignments_count = CanvasAssignment.objects.filter(
        course__user=request.user, is_active=True, published=True
    ).count()
    subaccounts = CanvasSubAccount.objects.select_related("user").filter(owner=request.user)
    sync_start_dt = credential.sync_start_at or _default_sync_start()
    now_local = timezone.localtime(timezone.now())
    return render(
        request,
        "directory/admin_dashboard.html",
        {
            "canvas_url": settings.CANVAS_URL,
            "credential": credential,
            "manageable_accounts": manageable_accounts,
            "accounts_error": accounts_error,
            "courses_count": courses_count,
            "assignments_count": assignments_count,
            "subaccounts": subaccounts,
            "sync_start_at_input": _as_datetime_local_value(sync_start_dt),
            "sync_start_at_max": now_local.strftime("%Y-%m-%dT%H:%M"),
        },
    )


@require_POST
@owner_account_required
def canvas_settings_save(request):
    token = (request.POST.get("token") or "").strip()
    credential, _ = CanvasCredential.objects.get_or_create(user=request.user)
    error = ""
    success = ""
    if token:
        try:
            client = CanvasClient(settings.CANVAS_URL, token)
            client.validate_token()
            credential.token = token
            credential.token_last_validated_at = timezone.now()
            credential.last_error = ""
            credential.save(
                update_fields=["token", "token_last_validated_at", "last_error", "updated_at"]
            )
            success = "Canvas token saved and validated."
        except CanvasClientError as exc:
            error = str(exc)
    else:
        error = "Token is required."

    if success:
        messages.success(request, success, extra_tags="canvas_settings")
    if error:
        messages.error(request, error, extra_tags="canvas_settings")
    return _admin_or_assignments_redirect(request)


@require_POST
@app_user_required
def canvas_sync(request):
    canvas_user = _effective_canvas_user(request.user)
    credential, _ = CanvasCredential.objects.get_or_create(user=canvas_user)
    if not credential.token:
        messages.error(request, "Add and validate your Canvas token first.")
        return _admin_or_assignments_redirect(request)
    if _related_sync_in_progress(request.user):
        messages.error(request, "A sync is already running for this account group.")
        return redirect("canvas_assignments")
    credential.sync_status = "queued"
    credential.sync_total_courses = 0
    credential.sync_processed_courses = 0
    credential.sync_current_course_name = ""
    credential.sync_progress_note = ""
    credential.sync_stop_requested = False
    credential.last_error = ""
    credential.save(
        update_fields=[
            "sync_status",
            "sync_total_courses",
            "sync_processed_courses",
            "sync_current_course_name",
            "sync_progress_note",
            "sync_stop_requested",
            "last_error",
            "updated_at",
        ]
    )
    sync_canvas_for_user.delay(canvas_user.id)
    return redirect("canvas_assignments")


@require_POST
@app_user_required
def canvas_sync_kick(request):
    canvas_user = _effective_canvas_user(request.user)
    credential, _ = CanvasCredential.objects.get_or_create(user=canvas_user)
    if not credential.token:
        messages.error(request, "Add and validate your Canvas token first.")
        return _admin_or_assignments_redirect(request)

    now = timezone.now()
    related_user_ids = _related_canvas_user_ids(request.user)
    reset_count = (
        CanvasCredential.objects.filter(
            user_id__in=related_user_ids,
            sync_status__in=["queued", "running"],
        )
        .exclude(user_id=canvas_user.id)
        .update(
            sync_status="error",
            sync_total_courses=0,
            sync_processed_courses=0,
            sync_current_course_name="",
            sync_progress_note="",
            sync_stop_requested=False,
            last_error="Sync was manually restarted from the UI.",
            updated_at=now,
        )
    )

    credential.sync_status = "queued"
    credential.sync_total_courses = 0
    credential.sync_processed_courses = 0
    credential.sync_current_course_name = ""
    credential.sync_progress_note = ""
    credential.sync_stop_requested = False
    credential.last_error = ""
    credential.save(
        update_fields=[
            "sync_status",
            "sync_total_courses",
            "sync_processed_courses",
            "sync_current_course_name",
            "sync_progress_note",
            "sync_stop_requested",
            "last_error",
            "updated_at",
        ]
    )
    sync_canvas_for_user.delay(canvas_user.id)

    if reset_count:
        messages.success(request, f"Sent a sync restart and reset {reset_count} stuck sync lock(s).")
    else:
        messages.success(request, "Sent a sync restart.")
    return redirect("canvas_assignments")


@require_POST
@app_user_required
def canvas_sync_stop(request):
    canvas_user = _effective_canvas_user(request.user)
    credential, _ = CanvasCredential.objects.get_or_create(user=canvas_user)
    if credential.sync_status in {"queued", "running"}:
        credential.sync_stop_requested = True
        credential.sync_progress_note = "Stopping sync after current course..."
        credential.save(update_fields=["sync_stop_requested", "sync_progress_note", "updated_at"])
        messages.success(request, "Stop requested.")
    else:
        messages.info(request, "No running sync to stop.")
    return redirect("canvas_assignments")


@require_POST
@owner_account_required
def canvas_sync_source_save(request):
    credential, _ = CanvasCredential.objects.get_or_create(user=request.user)
    sync_source = (request.POST.get("sync_source") or "enrolled").strip()
    account_id_raw = (request.POST.get("admin_account_id") or "").strip()

    if sync_source not in {"enrolled", "admin_account"}:
        messages.error(request, "Invalid sync source.", extra_tags="canvas_settings")
        return redirect("admin_dashboard")

    if sync_source == "enrolled":
        credential.sync_source = "enrolled"
        credential.admin_account_id = None
        credential.admin_account_name = ""
        credential.save(update_fields=["sync_source", "admin_account_id", "admin_account_name", "updated_at"])
        messages.success(request, "Sync source set to enrolled courses.", extra_tags="canvas_settings")
        return redirect("admin_dashboard")

    if not credential.token:
        messages.error(
            request,
            "Save and validate your Canvas token before choosing an admin account.",
            extra_tags="canvas_settings",
        )
        return redirect("admin_dashboard")

    try:
        selected_account_id = int(account_id_raw)
    except (TypeError, ValueError):
        messages.error(request, "Choose a valid admin account.", extra_tags="canvas_settings")
        return redirect("admin_dashboard")

    try:
        client = CanvasClient(settings.CANVAS_URL, credential.token)
        accounts = _load_available_accounts(client)
    except CanvasClientError as exc:
        messages.error(request, str(exc), extra_tags="canvas_settings")
        return redirect("admin_dashboard")

    selected_account = None
    for account in accounts:
        if int(account.get("id") or 0) == selected_account_id:
            selected_account = account
            break
    if not selected_account:
        messages.error(
            request,
            "Selected account is not in your manageable accounts.",
            extra_tags="canvas_settings",
        )
        return redirect("admin_dashboard")

    credential.sync_source = "admin_account"
    credential.admin_account_id = selected_account_id
    credential.admin_account_name = (selected_account.get("name") or "")[:255]
    credential.save(update_fields=["sync_source", "admin_account_id", "admin_account_name", "updated_at"])
    messages.success(
        request,
        f"Sync source set to admin account: {credential.admin_account_name or selected_account_id}",
        extra_tags="canvas_settings",
    )
    return redirect("admin_dashboard")


@require_POST
@owner_account_required
def canvas_sync_start_save(request):
    credential, _ = CanvasCredential.objects.get_or_create(user=request.user)
    raw = (request.POST.get("sync_start_at") or "").strip()
    if not raw:
        credential.sync_start_at = None
        credential.save(update_fields=["sync_start_at", "updated_at"])
        messages.success(
            request,
            "Sync start reset to default (previous September 1).",
            extra_tags="canvas_settings",
        )
        return redirect("admin_dashboard")

    parsed = parse_datetime(raw)
    if not parsed:
        messages.error(request, "Enter a valid date and time.", extra_tags="canvas_settings")
        return redirect("admin_dashboard")

    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, timezone.get_default_timezone())

    now = timezone.now()
    if parsed > now:
        messages.error(request, "Sync start must be in the past.", extra_tags="canvas_settings")
        return redirect("admin_dashboard")

    credential.sync_start_at = parsed
    credential.save(update_fields=["sync_start_at", "updated_at"])
    messages.success(
        request,
        f"Sync start updated to {timezone.localtime(parsed).strftime('%Y-%m-%d %H:%M')}.",
        extra_tags="canvas_settings",
    )
    return redirect("admin_dashboard")


@require_GET
@app_user_required
def canvas_sync_progress(request):
    canvas_user = _effective_canvas_user(request.user)
    credential, _ = CanvasCredential.objects.get_or_create(user=canvas_user)
    sync_locked = _related_sync_in_progress(request.user)
    total = max(int(credential.sync_total_courses or 0), 0)
    processed = max(int(credential.sync_processed_courses or 0), 0)
    if total > 0:
        percent = int((processed / total) * 100)
        if percent > 100:
            percent = 100
    else:
        percent = 0
    return JsonResponse(
        {
            "status": credential.sync_status or "never",
            "total_courses": total,
            "processed_courses": processed,
            "current_course_name": credential.sync_current_course_name or "",
            "progress_note": credential.sync_progress_note or "",
            "percent": percent,
            "last_error": credential.last_error or "",
            "sync_locked": sync_locked,
            "stop_requested": bool(credential.sync_stop_requested),
        }
    )


@require_POST
@owner_account_required
def canvas_burn_everything(request):
    user = request.user
    deleted_submission_reports, _ = CanvasSubmissionReport.objects.filter(user=user).delete()
    deleted_staff_marking_reports, _ = CanvasStaffMarkingReport.objects.filter(user=user).delete()
    deleted_moderation_reports, _ = CanvasAssignmentModerationReport.objects.filter(user=user).delete()
    deleted_reviews, _ = CanvasModerationSubmissionReview.objects.filter(user=user).delete()
    deleted_preferences, _ = CanvasModerationAssignmentPreference.objects.filter(user=user).delete()
    deleted_assignments, _ = CanvasAssignment.objects.filter(course__user=user).delete()
    deleted_courses, _ = CanvasCourse.objects.filter(user=user).delete()
    messages.success(
        request,
        "Burn complete. "
        f"Deleted {deleted_courses} courses, "
        f"{deleted_assignments} assignments, "
        f"{deleted_submission_reports} submission reports, "
        f"{deleted_staff_marking_reports} staff marking reports, "
        f"{deleted_moderation_reports} moderation reports, "
        f"{deleted_reviews} moderation review rows, "
        f"and {deleted_preferences} moderation preference rows for your account.",
        extra_tags="danger_zone",
    )
    return redirect("admin_dashboard")


@require_POST
@owner_account_required
def canvas_subaccounts_maintenance_toggle(request):
    credential, _ = CanvasCredential.objects.get_or_create(user=request.user)
    enable_requested = (request.POST.get("enable") or "").strip() == "1"
    credential.subaccounts_maintenance_mode = enable_requested
    credential.save(update_fields=["subaccounts_maintenance_mode", "updated_at"])
    if enable_requested:
        messages.success(request, "Sub-account maintenance mode enabled.", extra_tags="danger_zone")
    else:
        messages.success(request, "Sub-account maintenance mode disabled.", extra_tags="danger_zone")
    return redirect("admin_dashboard")


@require_POST
@owner_account_required
def admin_subaccount_create(request):
    username = (request.POST.get("username") or "").strip()
    if not username:
        messages.error(request, "Username is required.", extra_tags="subaccounts")
        return redirect("admin_dashboard")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{3,150}", username):
        messages.error(
            request,
            "Username must be 3-150 chars and only contain letters, numbers, underscore, dot, or hyphen.",
            extra_tags="subaccounts",
        )
        return redirect("admin_dashboard")
    if User.objects.filter(username__iexact=username).exists():
        messages.error(request, f"Username '{username}' is already in use.", extra_tags="subaccounts")
        return redirect("admin_dashboard")

    password = _generate_memorable_password()
    sub_user = User.objects.create_user(
        username=username,
        password=password,
        is_staff=False,
        is_active=True,
    )
    CanvasSubAccount.objects.create(owner=request.user, user=sub_user)
    messages.success(
        request,
        f"Created sub-account '{username}'. Temporary password: {password}",
        extra_tags="subaccounts",
    )
    return redirect("admin_dashboard")


@require_POST
@owner_account_required
def admin_subaccount_reset_password(request, subaccount_id):
    subaccount = get_object_or_404(
        CanvasSubAccount.objects.select_related("user"),
        id=subaccount_id,
        owner=request.user,
    )
    new_password = _generate_memorable_password()
    subaccount.user.set_password(new_password)
    subaccount.user.save(update_fields=["password"])
    messages.success(
        request,
        f"Reset password for '{subaccount.user.username}'. New temporary password: {new_password}",
        extra_tags=f"subaccounts subuser_{subaccount.user.username}",
    )
    return redirect("admin_dashboard")


@require_POST
@owner_account_required
def admin_subaccount_toggle_active(request, subaccount_id):
    subaccount = get_object_or_404(
        CanvasSubAccount.objects.select_related("user"),
        id=subaccount_id,
        owner=request.user,
    )
    subaccount.user.is_active = not subaccount.user.is_active
    subaccount.user.save(update_fields=["is_active"])
    state = "active" if subaccount.user.is_active else "disabled"
    messages.success(
        request,
        f"Sub-account '{subaccount.user.username}' is now {state}.",
        extra_tags=f"subaccounts subuser_{subaccount.user.username}",
    )
    return redirect("admin_dashboard")


@require_POST
@owner_account_required
def admin_subaccount_delete(request, subaccount_id):
    subaccount = get_object_or_404(
        CanvasSubAccount.objects.select_related("user"),
        id=subaccount_id,
        owner=request.user,
    )
    username = subaccount.user.username
    subaccount.user.delete()
    messages.success(
        request,
        f"Deleted sub-account '{username}'.",
        extra_tags="subaccounts",
    )
    return redirect("admin_dashboard")


@require_GET
@app_user_required
def canvas_assignments(request):
    canvas_user = _effective_canvas_user(request.user)
    _purge_expired_submission_reports(canvas_user)
    payload = _build_canvas_assignments_context(request)
    credential, _ = CanvasCredential.objects.get_or_create(user=canvas_user)
    reports, active_report = _reports_for_user(canvas_user)
    sync_locked = _related_sync_in_progress(request.user)

    return render(
        request,
        "directory/canvas_assignments.html",
        {
            **payload,
            "canvas_url": settings.CANVAS_URL,
            "credential": credential,
            "reports": reports,
            "active_report": active_report,
            "can_access_admin": not _is_subaccount_user(request.user),
            "sync_locked": sync_locked,
        },
    )


@require_GET
@app_user_required
def canvas_assignments_data(request):
    payload = _build_canvas_assignments_context(request)
    table_html = render_to_string(
        "directory/_canvas_assignments_table.html",
        {"assignments": payload["assignments"]},
        request=request,
    )
    return JsonResponse(
        {
            "table_html": table_html,
            "has_active_filters": payload["has_active_filters"],
            "count": len(payload["assignments"]),
        }
    )


@require_GET
@app_user_required
def canvas_assignment_moderate(request, assignment_id):
    canvas_user = _effective_canvas_user(request.user)
    assignment = get_object_or_404(
        CanvasAssignment.objects.select_related("course"),
        id=assignment_id,
        course__user=canvas_user,
        course__is_active=True,
        is_active=True,
        published=True,
    )
    credential, _ = CanvasCredential.objects.get_or_create(user=canvas_user)
    if not credential.token:
        messages.error(request, "Add and validate your Canvas token first.")
        return _admin_or_assignments_redirect(request)

    reports_qs = CanvasAssignmentModerationReport.objects.filter(
        user=canvas_user,
        assignment=assignment,
    ).order_by("-created_at")
    reports = list(reports_qs[:50])
    selected_report_id = (request.GET.get("report") or "").strip()
    active_report = next((r for r in reports if r.status in {"pending", "running"}), None)

    moderation_report = None
    if selected_report_id:
        try:
            selected_id = int(selected_report_id)
        except (TypeError, ValueError):
            selected_id = None
        if selected_id is not None:
            moderation_report = next((r for r in reports if r.id == selected_id), None)

    if moderation_report is None and active_report is not None:
        moderation_report = active_report
    if moderation_report is None and reports:
        moderation_report = reports[0]
    if moderation_report is None:
        moderation_report = CanvasAssignmentModerationReport.objects.create(
            user=canvas_user,
            assignment=assignment,
            status="pending",
        )
        generate_assignment_moderation_report.delay(moderation_report.id)
        reports = [moderation_report]

    return render(
        request,
        "directory/canvas_assignment_moderate.html",
        {
            "canvas_url": settings.CANVAS_URL,
            "assignment": assignment,
            "moderation_report": moderation_report,
            "moderation_reports": reports,
            "active_report": active_report,
        },
    )


@require_POST
@app_user_required
def canvas_assignment_moderate_regenerate(request, assignment_id):
    canvas_user = _effective_canvas_user(request.user)
    assignment = get_object_or_404(
        CanvasAssignment.objects.select_related("course"),
        id=assignment_id,
        course__user=canvas_user,
        course__is_active=True,
        is_active=True,
        published=True,
    )
    credential, _ = CanvasCredential.objects.get_or_create(user=canvas_user)
    if not credential.token:
        messages.error(request, "Add and validate your Canvas token first.")
        return _admin_or_assignments_redirect(request)

    active_report = CanvasAssignmentModerationReport.objects.filter(
        user=canvas_user,
        assignment=assignment,
        status__in=["pending", "running"],
    ).order_by("-created_at").first()
    if active_report:
        messages.warning(
            request,
            f"Report #{active_report.id} is already {active_report.status}. Wait for it to finish before regenerating.",
        )
        return redirect(f"/canvas/assignments/{assignment.id}/moderate/?report={active_report.id}")

    new_report = CanvasAssignmentModerationReport.objects.create(
        user=canvas_user,
        assignment=assignment,
        status="pending",
    )
    generate_assignment_moderation_report.delay(new_report.id)
    messages.success(request, f"Started new moderation report #{new_report.id}.")
    return redirect(f"/canvas/assignments/{assignment.id}/moderate/?report={new_report.id}")


@require_POST
@app_user_required
def canvas_assignment_moderate_delete(request, report_id):
    canvas_user = _effective_canvas_user(request.user)
    report = get_object_or_404(
        CanvasAssignmentModerationReport.objects.select_related("assignment"),
        id=report_id,
        user=canvas_user,
    )
    assignment_id = report.assignment_id
    if report.status in {"pending", "running"}:
        messages.error(request, f"Report #{report.id} is still {report.status}; wait for completion first.")
        return redirect(f"/canvas/assignments/{assignment_id}/moderate/?report={report.id}")

    report.delete()
    messages.success(request, f"Deleted moderation report #{report_id}.")
    return redirect(f"/canvas/assignments/{assignment_id}/moderate/")


@require_GET
@app_user_required
def canvas_assignment_moderate_progress(request, report_id):
    canvas_user = _effective_canvas_user(request.user)
    report = get_object_or_404(
        CanvasAssignmentModerationReport.objects.select_related("assignment", "assignment__course"),
        id=report_id,
        user=canvas_user,
    )
    total = max(int(report.total_submissions or 0), 0)
    processed = max(int(report.processed_submissions or 0), 0)
    stats_payload = dict(report.stats or {})
    _inject_review_state(report, stats_payload)
    progress = (stats_payload.get("_progress") or {}) if isinstance(stats_payload, dict) else {}
    extraction_processed = max(int(progress.get("extraction_processed") or 0), 0)
    extraction_total = max(int(progress.get("extraction_total") or 0), 0)
    processing_processed = max(int(progress.get("processing_processed") or 0), 0)
    processing_total = max(int(progress.get("processing_total") or 0), 0)

    extraction_percent = 0 if extraction_total <= 0 else int((extraction_processed / extraction_total) * 100)
    extraction_percent = min(max(extraction_percent, 0), 100)
    processing_percent = 0 if processing_total <= 0 else int((processing_processed / processing_total) * 100)
    processing_percent = min(max(processing_percent, 0), 100)

    if report.status == "completed":
        extraction_percent = 100
        processing_percent = 100
        percent = 100
    else:
        percent = int(progress.get("overall_percent") or int((extraction_percent + processing_percent) / 2))
        percent = min(max(percent, 0), 100)

    return JsonResponse(
        {
            "id": report.id,
            "status": report.status,
            "active": report.status in {"pending", "running"},
            "processed_submissions": processed,
            "total_submissions": total,
            "percent": percent,
            "phase": progress.get("phase") or "",
            "extraction_processed": extraction_processed,
            "extraction_total": extraction_total,
            "extraction_percent": extraction_percent,
            "processing_processed": processing_processed,
            "processing_total": processing_total,
            "processing_percent": processing_percent,
            "stats": stats_payload,
            "error": report.error or "",
            "assignment_id": report.assignment_id,
        }
    )


def _inject_review_state(report, stats_payload):
    fail_threshold = _get_fail_threshold(report.user_id, report.assignment_id)
    stats_payload["selected_fail_threshold"] = fail_threshold

    graded_submissions = stats_payload.get("graded_submissions") or []
    total_submissions = int(stats_payload.get("submissions_count") or 0)
    is_percentage = bool(stats_payload.get("is_percentage"))
    if graded_submissions:
        stats_payload["checked_submissions"] = _build_checked_submissions(
            graded_submissions=graded_submissions,
            total_submissions=total_submissions,
            report_id=report.id,
            is_percentage=is_percentage,
            fail_threshold=fail_threshold,
        )

    reviews = CanvasModerationSubmissionReview.objects.filter(
        user=report.user,
        assignment=report.assignment,
    )
    review_map = {}
    checked_count = 0
    issues_count = 0
    for review in reviews:
        key = str(review.submission_id)
        review_map[key] = {
            "notes": review.notes or "",
            "is_checked": bool(review.is_checked),
            "has_issue": bool(review.has_issue),
            "updated_at": review.updated_at.isoformat(),
        }
        if review.is_checked:
            checked_count += 1
        if review.has_issue:
            issues_count += 1

    stats_payload["checked_submission_reviews"] = review_map
    stats_payload["consolidated_review_counts"] = {
        "checked_count": checked_count,
        "issues_count": issues_count,
    }


def _get_fail_threshold(user_id, assignment_id):
    pref = CanvasModerationAssignmentPreference.objects.filter(
        user_id=user_id,
        assignment_id=assignment_id,
    ).first()
    if not pref:
        return 40.0
    return _normalize_fail_threshold(pref.fail_threshold)


def _normalize_fail_threshold(value):
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return 40.0
    return 50.0 if numeric >= 45.0 else 40.0


@require_POST
@app_user_required
def canvas_assignment_moderate_save_review(request, report_id):
    canvas_user = _effective_canvas_user(request.user)
    report = get_object_or_404(
        CanvasAssignmentModerationReport.objects.select_related("assignment"),
        id=report_id,
        user=canvas_user,
    )
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({"ok": False, "error": "Invalid JSON payload."}, status=400)

    submission_id = payload.get("submission_id")
    if submission_id is None:
        return JsonResponse({"ok": False, "error": "submission_id is required."}, status=400)
    try:
        submission_id_int = int(submission_id)
    except (TypeError, ValueError):
        return JsonResponse({"ok": False, "error": "submission_id must be an integer."}, status=400)

    notes = (payload.get("notes") or "").strip()
    is_checked = bool(payload.get("is_checked"))
    has_issue = bool(payload.get("has_issue"))

    student_id_raw = payload.get("student_id")
    try:
        student_id = int(student_id_raw) if student_id_raw is not None else None
    except (TypeError, ValueError):
        student_id = None

    student_name = (payload.get("student_name") or "")[:255]
    grader_name = (payload.get("grader_name") or "")[:255]
    score_raw = payload.get("score")
    try:
        score = float(score_raw) if score_raw is not None else None
    except (TypeError, ValueError):
        score = None

    with transaction.atomic():
        review, _ = CanvasModerationSubmissionReview.objects.update_or_create(
            user=canvas_user,
            assignment=report.assignment,
            submission_id=submission_id_int,
            defaults={
                "report": report,
                "student_id": student_id,
                "student_name": student_name,
                "grader_name": grader_name,
                "score": score,
                "notes": notes,
                "is_checked": is_checked,
                "has_issue": has_issue,
            },
        )

    consolidated = CanvasModerationSubmissionReview.objects.filter(
        user=canvas_user,
        assignment=report.assignment,
    )
    checked_count = consolidated.filter(is_checked=True).count()
    issues_count = consolidated.filter(has_issue=True).count()
    return JsonResponse(
        {
            "ok": True,
            "review": {
                "submission_id": review.submission_id,
                "notes": review.notes,
                "is_checked": review.is_checked,
                "has_issue": review.has_issue,
                "updated_at": review.updated_at.isoformat(),
            },
            "consolidated_review_counts": {
                "checked_count": checked_count,
                "issues_count": issues_count,
            },
        }
    )


@require_POST
@app_user_required
def canvas_assignment_moderate_save_threshold(request, report_id):
    canvas_user = _effective_canvas_user(request.user)
    report = get_object_or_404(
        CanvasAssignmentModerationReport.objects.select_related("assignment"),
        id=report_id,
        user=canvas_user,
    )
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({"ok": False, "error": "Invalid JSON payload."}, status=400)

    raw_value = payload.get("fail_threshold")
    try:
        fail_threshold = float(raw_value)
    except (TypeError, ValueError):
        return JsonResponse({"ok": False, "error": "fail_threshold must be numeric."}, status=400)
    if fail_threshold not in {40.0, 50.0}:
        return JsonResponse({"ok": False, "error": "fail_threshold must be 40 or 50."}, status=400)

    CanvasModerationAssignmentPreference.objects.update_or_create(
        user=canvas_user,
        assignment=report.assignment,
        defaults={"fail_threshold": fail_threshold},
    )

    stats_payload = dict(report.stats or {})
    _inject_review_state(report, stats_payload)
    return JsonResponse(
        {
            "ok": True,
            "fail_threshold": fail_threshold,
            "checked_submissions": stats_payload.get("checked_submissions") or [],
            "checked_submission_reviews": stats_payload.get("checked_submission_reviews") or {},
            "consolidated_review_counts": stats_payload.get("consolidated_review_counts") or {
                "checked_count": 0,
                "issues_count": 0,
            },
        }
    )


@require_GET
@app_user_required
def canvas_reports_table(request):
    canvas_user = _effective_canvas_user(request.user)
    _purge_expired_submission_reports(canvas_user)
    reports, _ = _reports_for_user(canvas_user)
    table_html = render_to_string(
        "directory/_canvas_reports_table.html",
        {"reports": reports},
        request=request,
    )
    return JsonResponse(
        {
            "table_html": table_html,
            "has_reports": bool(reports),
        }
    )


@require_POST
@app_user_required
def canvas_reports_create(request):
    canvas_user = _effective_canvas_user(request.user)
    _purge_expired_submission_reports(canvas_user)
    is_ajax = _is_ajax_request(request)
    active_kind, _ = _active_report_objects_for_user(canvas_user)
    if active_kind:
        error_msg = "A report is already running or queued. Wait for it to finish first."
        if is_ajax:
            return JsonResponse({"ok": False, "error": error_msg}, status=409)
        messages.error(request, error_msg)
        return redirect("canvas_assignments")

    filters = _report_filters_from_post(request)
    invalid_response = _validate_isolated_assignment_filter(
        request, canvas_user, filters, is_ajax=is_ajax
    )
    if invalid_response is not None:
        return invalid_response
    report = CanvasSubmissionReport.objects.create(
        user=canvas_user,
        status="pending",
        filters=filters,
    )
    generate_submissions_report.delay(report.id)
    if is_ajax:
        return JsonResponse(
            {
                "ok": True,
                "report": {
                    "id": report.id,
                    "kind": "submissions",
                    "kind_label": "Submissions",
                    "status": report.status,
                    "total_assignments": report.total_assignments,
                    "processed_assignments": report.processed_assignments,
                    "current_assignment_name": report.current_assignment_name or "",
                    "percent": 0,
                },
            }
        )
    messages.success(request, f"Report #{report.id} queued.")
    return redirect("canvas_assignments")


@require_POST
@app_user_required
def canvas_staff_marking_reports_create(request):
    canvas_user = _effective_canvas_user(request.user)
    _purge_expired_submission_reports(canvas_user)
    is_ajax = _is_ajax_request(request)
    active_kind, _ = _active_report_objects_for_user(canvas_user)
    if active_kind:
        error_msg = "A report is already running or queued. Wait for it to finish first."
        if is_ajax:
            return JsonResponse({"ok": False, "error": error_msg}, status=409)
        messages.error(request, error_msg)
        return redirect("canvas_assignments")

    filters = _report_filters_from_post(request)
    invalid_response = _validate_isolated_assignment_filter(
        request, canvas_user, filters, is_ajax=is_ajax
    )
    if invalid_response is not None:
        return invalid_response
    report = CanvasStaffMarkingReport.objects.create(
        user=canvas_user,
        status="pending",
        filters=filters,
    )
    generate_staff_marking_report.delay(report.id)
    if is_ajax:
        return JsonResponse(
            {
                "ok": True,
                "report": {
                    "id": report.id,
                    "kind": "staff_marking",
                    "kind_label": "Staff marking",
                    "status": report.status,
                    "total_assignments": report.total_assignments,
                    "processed_assignments": report.processed_assignments,
                    "current_assignment_name": report.current_assignment_name or "",
                    "percent": 0,
                },
            }
        )
    messages.success(request, f"Staff marking report #{report.id} queued.")
    return redirect("canvas_assignments")


def _report_filters_from_post(request):
    return {
        "course": (request.POST.get("course") or "").strip(),
        "course_name": (request.POST.get("course_name") or "").strip(),
        "assignment_type": (request.POST.get("assignment_type") or "").strip(),
        "rubric_criterion": (request.POST.get("rubric_criterion") or "").strip(),
        "enrolled": (request.POST.get("enrolled") or "all").strip().lower(),
        "needs_grading": (request.POST.get("needs_grading") or "all").strip().lower(),
        "assignment_name": (request.POST.get("assignment_name") or "").strip(),
        "date_from": (request.POST.get("date_from") or "").strip(),
        "date_to": (request.POST.get("date_to") or "").strip(),
        "isolated_assignment_id": (request.POST.get("isolated_assignment_id") or "").strip(),
    }


def _validate_isolated_assignment_filter(request, canvas_user, filters, *, is_ajax=False):
    isolated_assignment_id_raw = filters.get("isolated_assignment_id") or ""
    if isolated_assignment_id_raw:
        try:
            isolated_assignment_id = int(isolated_assignment_id_raw)
        except (TypeError, ValueError):
            error_msg = "Invalid isolated assignment."
            if is_ajax:
                return JsonResponse({"ok": False, "error": error_msg}, status=400)
            messages.error(request, error_msg)
            return redirect("canvas_assignments")
        exists = CanvasAssignment.objects.filter(
            id=isolated_assignment_id,
            course__user=canvas_user,
            course__is_active=True,
            is_active=True,
            published=True,
        ).exists()
        if not exists:
            error_msg = "Isolated assignment is no longer available."
            if is_ajax:
                return JsonResponse({"ok": False, "error": error_msg}, status=400)
            messages.error(request, error_msg)
            return redirect("canvas_assignments")
    return None


@require_POST
@app_user_required
def canvas_report_cancel(request, report_id):
    canvas_user = _effective_canvas_user(request.user)
    _purge_expired_submission_reports(canvas_user)
    report = get_object_or_404(CanvasSubmissionReport, id=report_id, user=canvas_user)
    if report.status in {"pending", "running"}:
        report.cancel_requested = True
        if report.status == "pending":
            report.status = "cancelled"
            report.completed_at = timezone.now()
            report.save(update_fields=["cancel_requested", "status", "completed_at"])
        else:
            report.save(update_fields=["cancel_requested"])
        messages.success(request, f"Cancel requested for report #{report.id}.")
    return redirect("canvas_assignments")


@require_POST
@app_user_required
def canvas_report_delete(request, report_id):
    canvas_user = _effective_canvas_user(request.user)
    _purge_expired_submission_reports(canvas_user)
    report = get_object_or_404(CanvasSubmissionReport, id=report_id, user=canvas_user)
    if report.status in {"pending", "running"}:
        messages.error(request, f"Report #{report.id} is still {report.status}; cancel it first.")
        return redirect("canvas_assignments")
    report.delete()
    messages.success(request, f"Deleted report #{report_id}.")
    return redirect("canvas_assignments")


@require_GET
@app_user_required
def canvas_report_download(request, report_id):
    canvas_user = _effective_canvas_user(request.user)
    _purge_expired_submission_reports(canvas_user)
    report = get_object_or_404(CanvasSubmissionReport, id=report_id, user=canvas_user)
    if report.status != "completed" or not report.csv_content:
        return HttpResponse("Report is not ready.", status=400)

    response = HttpResponse(report.csv_content, content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="submissions-report-{report.id}.csv"'
    return response


@require_GET
@app_user_required
def canvas_report_progress(request):
    canvas_user = _effective_canvas_user(request.user)
    _purge_expired_submission_reports(canvas_user)
    report_kind, report = _active_report_objects_for_user(canvas_user)
    if report is None:
        return JsonResponse({"active": False})
    total = max(int(report.total_assignments or 0), 0)
    processed = max(int(report.processed_assignments or 0), 0)
    percent = int((processed / total) * 100) if total > 0 else 0
    if percent > 100:
        percent = 100
    return JsonResponse(
        {
            "active": True,
            "id": report.id,
            "kind": report_kind,
            "kind_label": "Staff marking" if report_kind == "staff_marking" else "Submissions",
            "status": report.status,
            "total_assignments": total,
            "processed_assignments": processed,
            "current_assignment_name": report.current_assignment_name or "",
            "percent": percent,
            "cancel_requested": report.cancel_requested,
        }
    )


@require_POST
@app_user_required
def canvas_staff_marking_report_cancel(request, report_id):
    canvas_user = _effective_canvas_user(request.user)
    _purge_expired_submission_reports(canvas_user)
    report = get_object_or_404(CanvasStaffMarkingReport, id=report_id, user=canvas_user)
    if report.status in {"pending", "running"}:
        report.cancel_requested = True
        if report.status == "pending":
            report.status = "cancelled"
            report.completed_at = timezone.now()
            report.save(update_fields=["cancel_requested", "status", "completed_at"])
        else:
            report.save(update_fields=["cancel_requested"])
        messages.success(request, f"Cancel requested for staff marking report #{report.id}.")
    return redirect("canvas_assignments")


@require_POST
@app_user_required
def canvas_staff_marking_report_delete(request, report_id):
    canvas_user = _effective_canvas_user(request.user)
    _purge_expired_submission_reports(canvas_user)
    report = get_object_or_404(CanvasStaffMarkingReport, id=report_id, user=canvas_user)
    if report.status in {"pending", "running"}:
        messages.error(
            request,
            f"Staff marking report #{report.id} is still {report.status}; cancel it first.",
        )
        return redirect("canvas_assignments")
    report.delete()
    messages.success(request, f"Deleted staff marking report #{report_id}.")
    return redirect("canvas_assignments")


@require_GET
@app_user_required
def canvas_staff_marking_report_download(request, report_id):
    canvas_user = _effective_canvas_user(request.user)
    _purge_expired_submission_reports(canvas_user)
    report = get_object_or_404(CanvasStaffMarkingReport, id=report_id, user=canvas_user)
    if report.status != "completed" or not report.csv_content:
        return HttpResponse("Report is not ready.", status=400)

    response = HttpResponse(report.csv_content, content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="staff-marking-report-{report.id}.csv"'
    return response
