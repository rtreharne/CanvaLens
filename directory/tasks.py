from celery import shared_task
import re
from django.conf import settings
from django.utils import timezone as dj_timezone
from django.utils.dateparse import parse_date, parse_datetime
from datetime import datetime, time, timedelta
from django.db.models import Q
import csv
import io
from .models import (
    CanvasCredential,
    CanvasCourse,
    CanvasAssignment,
    CanvasSubmissionReport,
)
from .canvas_client import CanvasClient, CanvasClientError


@shared_task
def purge_expired_submission_reports():
    cutoff = dj_timezone.now() - timedelta(hours=1)
    deleted_count, _ = CanvasSubmissionReport.objects.filter(created_at__lt=cutoff).exclude(
        status__in=["pending", "running"]
    ).delete()
    return deleted_count


def _build_group_set_context(client, assignments):
    course_ids = sorted({a.course.canvas_id for a in assignments})
    group_set_names = set()
    membership_lookup = {}

    for course_id in course_ids:
        try:
            categories = client.list_course_group_categories(course_id)
        except CanvasClientError:
            continue

        for category in categories or []:
            category_id = category.get("id")
            if not category_id:
                continue
            group_set_name = (category.get("name") or f"group_set_{category_id}").strip()
            if not group_set_name:
                continue
            group_set_names.add(group_set_name)

            try:
                groups = client.list_group_category_groups(category_id)
            except CanvasClientError:
                continue

            for group in groups or []:
                group_id = group.get("id")
                if not group_id:
                    continue
                group_name = (group.get("name") or f"group_{group_id}").strip()
                try:
                    users = client.list_group_users(group_id)
                except CanvasClientError:
                    continue
                for user in users or []:
                    user_id = user.get("id")
                    if not user_id:
                        continue
                    try:
                        user_id_int = int(user_id)
                    except (TypeError, ValueError):
                        continue
                    key = (course_id, user_id_int, group_set_name)
                    membership_lookup.setdefault(key, set()).add(group_name)

    return sorted(group_set_names), membership_lookup


def _parse_canvas_dt(value):
    if not value:
        return None
    dt = parse_datetime(value)
    if not dt:
        return None
    if dt.tzinfo is None:
        return dj_timezone.make_aware(dt, dj_timezone.get_default_timezone())
    return dt


def _previous_september_window(now):
    tz = dj_timezone.get_default_timezone()
    sept_this_year = dj_timezone.make_aware(datetime(now.year, 9, 1, 0, 0, 0), tz)
    if now < sept_this_year:
        window_start = dj_timezone.make_aware(datetime(now.year - 1, 9, 1, 0, 0, 0), tz)
    else:
        window_start = sept_this_year
    window_end = dj_timezone.make_aware(datetime(window_start.year + 1, 9, 1, 0, 0, 0), tz)
    return window_start, window_end


def _assignment_in_window(assignment_data, window_start, window_end):
    unlock_at = _parse_canvas_dt((assignment_data or {}).get("unlock_at"))
    close_at = _parse_canvas_dt((assignment_data or {}).get("lock_at"))
    due_at = _parse_canvas_dt((assignment_data or {}).get("due_at"))
    for dt in (unlock_at, close_at, due_at):
        if dt and window_start <= dt < window_end:
            return True
    return False


def _parse_report_filter_dt(value):
    if not value:
        return None
    dt = parse_datetime(value)
    if dt:
        if dt.tzinfo is None:
            return dj_timezone.make_aware(dt, dj_timezone.get_default_timezone())
        return dt
    d = parse_date(value)
    if not d:
        return None
    return dj_timezone.make_aware(datetime.combine(d, time.min), dj_timezone.get_default_timezone())


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


def _filtered_assignments_for_report(user_id, filters):
    course_id = (filters.get("course") or "").strip()
    course_name = (filters.get("course_name") or "").strip()
    assignment_type = (filters.get("assignment_type") or "").strip()
    assignment_name = (filters.get("assignment_name") or "").strip()
    enrolled_filter = (filters.get("enrolled") or "all").strip().lower()
    date_from = _parse_report_filter_dt(filters.get("date_from"))
    date_to = _parse_report_filter_dt(filters.get("date_to"))

    assignments = CanvasAssignment.objects.select_related("course").filter(
        course__user_id=user_id,
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
    return assignments.order_by("due_at", "name")


@shared_task
def sync_canvas_for_user(user_id, existing_only=False):
    credential = CanvasCredential.objects.filter(user_id=user_id).first()
    if not credential or not credential.token:
        return

    client = CanvasClient(settings.CANVAS_URL, credential.token)
    credential.sync_status = "running"
    credential.last_error = ""
    credential.sync_total_courses = 0
    credential.sync_processed_courses = 0
    credential.sync_current_course_name = ""
    credential.save(
        update_fields=[
            "sync_status",
            "last_error",
            "sync_total_courses",
            "sync_processed_courses",
            "sync_current_course_name",
            "updated_at",
        ]
    )

    try:
        enrolled_courses = client.list_courses()
        enrolled_course_ids = {c.get("id") for c in enrolled_courses if c.get("id")}
        if credential.sync_source == "admin_account":
            if not credential.admin_account_id:
                credential.sync_status = "error"
                credential.last_error = "No admin account selected for admin account sync."
                credential.save(update_fields=["sync_status", "last_error", "updated_at"])
                return
            courses = client.list_account_courses(credential.admin_account_id)
        else:
            courses = enrolled_courses
        if existing_only:
            existing_course_ids = set(
                CanvasCourse.objects.filter(user_id=user_id).values_list("canvas_id", flat=True)
            )
            courses = [c for c in courses if c.get("id") in existing_course_ids]
        now = dj_timezone.now()
        window_start, window_end = _previous_september_window(now)
        seen_course_ids = set()
        eligible_courses = []

        for course_data in courses:
            canvas_course_id = course_data.get("id")
            if not canvas_course_id:
                continue
            eligible_courses.append(course_data)

        credential.sync_total_courses = len(eligible_courses)
        credential.sync_processed_courses = 0
        credential.sync_current_course_name = ""
        credential.save(
            update_fields=[
                "sync_total_courses",
                "sync_processed_courses",
                "sync_current_course_name",
                "updated_at",
            ]
        )

        for idx, course_data in enumerate(eligible_courses, start=1):
            canvas_course_id = course_data.get("id")
            credential.sync_current_course_name = (course_data.get("name") or f"Course {canvas_course_id}")[:255]
            credential.save(update_fields=["sync_current_course_name", "updated_at"])
            assignments = client.list_course_assignments(canvas_course_id)
            qualifying_assignments = []
            for assignment_data in assignments:
                if not assignment_data.get("published", False):
                    continue
                if _assignment_in_window(assignment_data, window_start, window_end):
                    qualifying_assignments.append(assignment_data)

            if not qualifying_assignments:
                credential.sync_processed_courses = idx
                credential.save(update_fields=["sync_processed_courses", "updated_at"])
                continue

            seen_course_ids.add(canvas_course_id)
            term = course_data.get("term") or {}
            course_obj, _ = CanvasCourse.objects.update_or_create(
                user_id=user_id,
                canvas_id=canvas_course_id,
                defaults={
                    "name": course_data.get("name") or f"Course {canvas_course_id}",
                    "is_enrolled": canvas_course_id in enrolled_course_ids,
                    "course_code": course_data.get("course_code") or "",
                    "workflow_state": course_data.get("workflow_state") or "",
                    "term_name": term.get("name") or "",
                    "start_at": _parse_canvas_dt(course_data.get("start_at")),
                    "end_at": _parse_canvas_dt(course_data.get("end_at")),
                    "is_active": True,
                    "raw_data": course_data,
                },
            )

            seen_assignment_ids = set()
            for assignment_data in qualifying_assignments:
                canvas_assignment_id = assignment_data.get("id")
                if not canvas_assignment_id:
                    continue
                seen_assignment_ids.add(canvas_assignment_id)
                CanvasAssignment.objects.update_or_create(
                    course=course_obj,
                    canvas_id=canvas_assignment_id,
                    defaults={
                        "name": assignment_data.get("name") or f"Assignment {canvas_assignment_id}",
                        "published": bool(assignment_data.get("published")),
                        "unlock_at": _parse_canvas_dt(assignment_data.get("unlock_at")),
                        "close_at": _parse_canvas_dt(assignment_data.get("lock_at")),
                        "due_at": _parse_canvas_dt(assignment_data.get("due_at")),
                        "submission_types": assignment_data.get("submission_types") or [],
                        "assignment_group_name": assignment_data.get("assignment_group_name") or "",
                        "points_possible": assignment_data.get("points_possible"),
                        "html_url": assignment_data.get("html_url") or "",
                        "is_active": True,
                        "raw_data": assignment_data,
                    },
                )

            CanvasAssignment.objects.filter(course=course_obj).exclude(
                canvas_id__in=seen_assignment_ids
            ).update(is_active=False)
            credential.sync_processed_courses = idx
            credential.save(update_fields=["sync_processed_courses", "updated_at"])

        CanvasCourse.objects.filter(user_id=user_id).exclude(canvas_id__in=seen_course_ids).update(is_active=False)
        CanvasAssignment.objects.filter(course__user_id=user_id, course__is_active=False).update(is_active=False)
        credential.sync_status = "ok"
        credential.last_sync_at = dj_timezone.now()
        credential.last_error = ""
        credential.sync_current_course_name = ""
        credential.save(
            update_fields=[
                "sync_status",
                "last_sync_at",
                "last_error",
                "sync_current_course_name",
                "updated_at",
            ]
        )
    except CanvasClientError as exc:
        credential.sync_status = "error"
        credential.last_error = str(exc)
        credential.sync_current_course_name = ""
        credential.save(
            update_fields=[
                "sync_status",
                "last_error",
                "sync_current_course_name",
                "updated_at",
            ]
        )


@shared_task
def generate_submissions_report(report_id):
    report = CanvasSubmissionReport.objects.select_related("user").filter(id=report_id).first()
    if not report:
        return

    credential = CanvasCredential.objects.filter(user=report.user).first()
    if not credential or not credential.token:
        report.status = "failed"
        report.error = "Canvas token not configured."
        report.completed_at = dj_timezone.now()
        report.save(update_fields=["status", "error", "completed_at"])
        return

    if report.cancel_requested:
        report.status = "cancelled"
        report.completed_at = dj_timezone.now()
        report.save(update_fields=["status", "completed_at"])
        return

    report.status = "running"
    report.started_at = dj_timezone.now()
    report.error = ""
    report.processed_assignments = 0
    report.current_assignment_name = ""
    report.save(
        update_fields=[
            "status",
            "started_at",
            "error",
            "processed_assignments",
            "current_assignment_name",
        ]
    )

    client = CanvasClient(settings.CANVAS_URL, credential.token)
    assignments = list(_filtered_assignments_for_report(report.user_id, report.filters))
    report.total_assignments = len(assignments)
    report.save(update_fields=["total_assignments"])

    group_set_columns, group_memberships = _build_group_set_context(client, assignments)

    output = io.StringIO()
    writer = csv.writer(output)
    header = [
        "course_id",
        "course_name",
        "assignment_id",
        "assignment_name",
        "submission_id",
        "user_id",
        "user_name",
        "workflow_state",
        "submitted_at",
        "score",
        "grade",
        "late",
        "missing",
        "excused",
        "attempt",
    ]
    header.extend(group_set_columns)
    writer.writerow(header)
    row_count = 0

    try:
        for idx, assignment in enumerate(assignments, start=1):
            report.refresh_from_db(fields=["cancel_requested"])
            if report.cancel_requested:
                report.status = "cancelled"
                report.completed_at = dj_timezone.now()
                report.current_assignment_name = ""
                report.save(update_fields=["status", "completed_at", "current_assignment_name"])
                return

            report.current_assignment_name = assignment.name[:255]
            report.processed_assignments = idx - 1
            report.save(update_fields=["current_assignment_name", "processed_assignments"])

            submissions = client.list_assignment_submissions(assignment.course.canvas_id, assignment.canvas_id)
            for submission in submissions or []:
                user = submission.get("user") or {}
                user_id = submission.get("user_id")
                try:
                    user_id_int = int(user_id) if user_id is not None else None
                except (TypeError, ValueError):
                    user_id_int = None
                group_values = []
                for group_set_name in group_set_columns:
                    if user_id_int is None:
                        group_values.append("")
                        continue
                    key = (assignment.course.canvas_id, user_id_int, group_set_name)
                    names = sorted(group_memberships.get(key, set()))
                    group_values.append("; ".join(names) if names else "")
                writer.writerow(
                    [
                        assignment.course.canvas_id,
                        assignment.course.name,
                        assignment.canvas_id,
                        assignment.name,
                        submission.get("id"),
                        user_id,
                        user.get("name") or "",
                        submission.get("workflow_state") or "",
                        submission.get("submitted_at") or "",
                        submission.get("score"),
                        submission.get("grade"),
                        submission.get("late"),
                        submission.get("missing"),
                        submission.get("excused"),
                        submission.get("attempt"),
                        *group_values,
                    ],
                )
                row_count += 1

            report.processed_assignments = idx
            report.save(update_fields=["processed_assignments"])

        report.status = "completed"
        report.completed_at = dj_timezone.now()
        report.current_assignment_name = ""
        report.csv_content = output.getvalue()
        report.row_count = row_count
        report.save(
            update_fields=[
                "status",
                "completed_at",
                "current_assignment_name",
                "csv_content",
                "row_count",
            ]
        )
    except CanvasClientError as exc:
        report.status = "failed"
        report.error = str(exc)
        report.completed_at = dj_timezone.now()
        report.current_assignment_name = ""
        report.save(update_fields=["status", "error", "completed_at", "current_assignment_name"])
    except Exception as exc:
        report.status = "failed"
        report.error = str(exc)
        report.completed_at = dj_timezone.now()
        report.current_assignment_name = ""
        report.save(update_fields=["status", "error", "completed_at", "current_assignment_name"])
