from functools import wraps
import re

from django.conf import settings
from django.contrib import messages
from django.http import HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime, parse_date
from datetime import datetime, time, timedelta
from django.views.decorators.http import require_GET, require_POST

from .canvas_client import CanvasClient, CanvasClientError
from .models import CanvasAssignment, CanvasCourse, CanvasCredential, CanvasSubmissionReport
from .tasks import generate_submissions_report, sync_canvas_for_user


def staff_required(view_func):
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect("login")
        if not request.user.is_staff:
            return HttpResponseForbidden("Staff access required.")
        return view_func(request, *args, **kwargs)

    return _wrapped


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


def _is_ajax_request(request):
    return request.headers.get("x-requested-with") == "XMLHttpRequest"


def _reports_for_user(user):
    reports_qs = CanvasSubmissionReport.objects.filter(user=user).order_by("-created_at")
    active_report = reports_qs.filter(status__in=["pending", "running"]).first()
    reports = list(reports_qs[:20])
    return reports, active_report


def _build_canvas_assignments_context(request):
    course_id = (request.GET.get("course") or "").strip()
    course_name = (request.GET.get("course_name") or "").strip()
    assignment_type = (request.GET.get("assignment_type") or "").strip()
    assignment_name = (request.GET.get("assignment_name") or "").strip()
    enrolled_filter = (request.GET.get("enrolled") or "all").strip().lower()

    date_from = _parse_filter_dt(request.GET.get("date_from"))
    date_to = _parse_filter_dt(request.GET.get("date_to"))

    courses = CanvasCourse.objects.filter(user=request.user, is_active=True).order_by("name")

    assignments = CanvasAssignment.objects.select_related("course").filter(
        course__user=request.user,
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

    assignment_type_values = set()
    for types in CanvasAssignment.objects.filter(
        course__user=request.user,
        is_active=True,
    ).values_list("submission_types", flat=True):
        for item in types or []:
            if item:
                assignment_type_values.add(item)

    selected = {
        "course": course_id,
        "course_name": course_name,
        "assignment_type": assignment_type,
        "enrolled": enrolled_filter,
        "assignment_name": assignment_name,
        "date_from": request.GET.get("date_from", ""),
        "date_to": request.GET.get("date_to", ""),
    }

    has_active_filters = bool(
        selected["course"]
        or selected["course_name"]
        or selected["assignment_type"]
        or selected["assignment_name"]
        or selected["date_from"]
        or selected["date_to"]
        or selected["enrolled"] != "all"
    )

    return {
        "courses": courses,
        "assignments": assignments.order_by("due_at", "name")[:500],
        "assignment_types": sorted(assignment_type_values),
        "selected": selected,
        "has_active_filters": has_active_filters,
    }


@require_GET
@staff_required
def index(request):
    return redirect("canvas_assignments")


@require_GET
@staff_required
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
        },
    )


@require_POST
@staff_required
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
        messages.success(request, success)
    if error:
        messages.error(request, error)
    return redirect("admin_dashboard")


@require_POST
@staff_required
def canvas_sync(request):
    credential, _ = CanvasCredential.objects.get_or_create(user=request.user)
    if not credential.token:
        messages.error(request, "Add and validate your Canvas token first.")
        return redirect("admin_dashboard")
    sync_mode = (request.POST.get("sync_mode") or "all").strip().lower()
    if sync_mode not in {"all", "existing"}:
        sync_mode = "all"
    credential.sync_status = "queued"
    credential.sync_total_courses = 0
    credential.sync_processed_courses = 0
    credential.sync_current_course_name = ""
    credential.last_error = ""
    credential.save(
        update_fields=[
            "sync_status",
            "sync_total_courses",
            "sync_processed_courses",
            "sync_current_course_name",
            "last_error",
            "updated_at",
        ]
    )
    sync_canvas_for_user.delay(request.user.id, existing_only=(sync_mode == "existing"))
    return redirect("canvas_assignments")


@require_POST
@staff_required
def canvas_sync_source_save(request):
    credential, _ = CanvasCredential.objects.get_or_create(user=request.user)
    sync_source = (request.POST.get("sync_source") or "enrolled").strip()
    account_id_raw = (request.POST.get("admin_account_id") or "").strip()

    if sync_source not in {"enrolled", "admin_account"}:
        messages.error(request, "Invalid sync source.")
        return redirect("admin_dashboard")

    if sync_source == "enrolled":
        credential.sync_source = "enrolled"
        credential.admin_account_id = None
        credential.admin_account_name = ""
        credential.save(update_fields=["sync_source", "admin_account_id", "admin_account_name", "updated_at"])
        messages.success(request, "Sync source set to enrolled courses.")
        return redirect("admin_dashboard")

    if not credential.token:
        messages.error(request, "Save and validate your Canvas token before choosing an admin account.")
        return redirect("admin_dashboard")

    try:
        selected_account_id = int(account_id_raw)
    except (TypeError, ValueError):
        messages.error(request, "Choose a valid admin account.")
        return redirect("admin_dashboard")

    try:
        client = CanvasClient(settings.CANVAS_URL, credential.token)
        accounts = _load_available_accounts(client)
    except CanvasClientError as exc:
        messages.error(request, str(exc))
        return redirect("admin_dashboard")

    selected_account = None
    for account in accounts:
        if int(account.get("id") or 0) == selected_account_id:
            selected_account = account
            break
    if not selected_account:
        messages.error(request, "Selected account is not in your manageable accounts.")
        return redirect("admin_dashboard")

    credential.sync_source = "admin_account"
    credential.admin_account_id = selected_account_id
    credential.admin_account_name = (selected_account.get("name") or "")[:255]
    credential.save(update_fields=["sync_source", "admin_account_id", "admin_account_name", "updated_at"])
    messages.success(request, f"Sync source set to admin account: {credential.admin_account_name or selected_account_id}")
    return redirect("admin_dashboard")


@require_GET
@staff_required
def canvas_sync_progress(request):
    credential, _ = CanvasCredential.objects.get_or_create(user=request.user)
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
            "percent": percent,
            "last_error": credential.last_error or "",
        }
    )


@require_POST
@staff_required
def canvas_burn_everything(request):
    deleted_assignments, _ = CanvasAssignment.objects.all().delete()
    deleted_courses, _ = CanvasCourse.objects.all().delete()
    messages.success(
        request,
        f"Burn complete. Deleted {deleted_courses} courses and {deleted_assignments} assignments.",
    )
    return redirect("admin_dashboard")


@require_GET
@staff_required
def canvas_assignments(request):
    _purge_expired_submission_reports(request.user)
    payload = _build_canvas_assignments_context(request)
    credential, _ = CanvasCredential.objects.get_or_create(user=request.user)
    reports, active_report = _reports_for_user(request.user)

    return render(
        request,
        "directory/canvas_assignments.html",
        {
            **payload,
            "canvas_url": settings.CANVAS_URL,
            "credential": credential,
            "reports": reports,
            "active_report": active_report,
        },
    )


@require_GET
@staff_required
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
@staff_required
def canvas_reports_table(request):
    _purge_expired_submission_reports(request.user)
    reports, _ = _reports_for_user(request.user)
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
@staff_required
def canvas_reports_create(request):
    _purge_expired_submission_reports(request.user)
    is_ajax = _is_ajax_request(request)
    active_exists = CanvasSubmissionReport.objects.filter(
        user=request.user, status__in=["pending", "running"]
    ).exists()
    if active_exists:
        error_msg = "A report is already running or queued. Wait for it to finish first."
        if is_ajax:
            return JsonResponse({"ok": False, "error": error_msg}, status=409)
        messages.error(request, error_msg)
        return redirect("canvas_assignments")

    filters = {
        "course": (request.POST.get("course") or "").strip(),
        "course_name": (request.POST.get("course_name") or "").strip(),
        "assignment_type": (request.POST.get("assignment_type") or "").strip(),
        "enrolled": (request.POST.get("enrolled") or "all").strip().lower(),
        "assignment_name": (request.POST.get("assignment_name") or "").strip(),
        "date_from": (request.POST.get("date_from") or "").strip(),
        "date_to": (request.POST.get("date_to") or "").strip(),
    }
    report = CanvasSubmissionReport.objects.create(
        user=request.user,
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
@staff_required
def canvas_report_cancel(request, report_id):
    _purge_expired_submission_reports(request.user)
    report = get_object_or_404(CanvasSubmissionReport, id=report_id, user=request.user)
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
@staff_required
def canvas_report_delete(request, report_id):
    _purge_expired_submission_reports(request.user)
    report = get_object_or_404(CanvasSubmissionReport, id=report_id, user=request.user)
    if report.status in {"pending", "running"}:
        messages.error(request, f"Report #{report.id} is still {report.status}; cancel it first.")
        return redirect("canvas_assignments")
    report.delete()
    messages.success(request, f"Deleted report #{report_id}.")
    return redirect("canvas_assignments")


@require_GET
@staff_required
def canvas_report_download(request, report_id):
    _purge_expired_submission_reports(request.user)
    report = get_object_or_404(CanvasSubmissionReport, id=report_id, user=request.user)
    if report.status != "completed" or not report.csv_content:
        return HttpResponse("Report is not ready.", status=400)

    response = HttpResponse(report.csv_content, content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="submissions-report-{report.id}.csv"'
    return response


@require_GET
@staff_required
def canvas_report_progress(request):
    _purge_expired_submission_reports(request.user)
    report = CanvasSubmissionReport.objects.filter(
        user=request.user, status__in=["pending", "running"]
    ).order_by("-created_at").first()
    if not report:
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
            "status": report.status,
            "total_assignments": total,
            "processed_assignments": processed,
            "current_assignment_name": report.current_assignment_name or "",
            "percent": percent,
            "cancel_requested": report.cancel_requested,
        }
    )
