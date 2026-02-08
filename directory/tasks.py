from celery import shared_task
import re
from django.conf import settings
from django.utils import timezone as dj_timezone
from django.utils.dateparse import parse_date, parse_datetime
from datetime import datetime, time, timedelta
from django.db.models import Q
import csv
import io
import math
import random
import statistics
from collections import defaultdict
from scipy import stats as scipy_stats
from .models import (
    CanvasCredential,
    CanvasCourse,
    CanvasAssignment,
    CanvasAssignmentModerationReport,
    CanvasModerationAssignmentPreference,
    CanvasStaffMarkingReport,
    CanvasSubmissionReport,
)
from .canvas_client import CanvasClient, CanvasClientError


@shared_task
def purge_expired_submission_reports():
    retention_hours = max(1, int(getattr(settings, "REPORT_RETENTION_HOURS", 24)))
    cutoff = dj_timezone.now() - timedelta(hours=retention_hours)
    deleted_submission_count, _ = CanvasSubmissionReport.objects.filter(created_at__lt=cutoff).exclude(
        status__in=["pending", "running"]
    ).delete()
    deleted_staff_marking_count, _ = CanvasStaffMarkingReport.objects.filter(created_at__lt=cutoff).exclude(
        status__in=["pending", "running"]
    ).delete()
    return deleted_submission_count + deleted_staff_marking_count


@shared_task
def generate_assignment_moderation_report(report_id):
    report = CanvasAssignmentModerationReport.objects.select_related(
        "user", "assignment", "assignment__course"
    ).filter(id=report_id).first()
    if not report:
        return

    credential = CanvasCredential.objects.filter(user=report.user).first()
    if not credential or not credential.token:
        report.status = "failed"
        report.error = "Canvas token not configured."
        report.completed_at = dj_timezone.now()
        report.save(update_fields=["status", "error", "completed_at"])
        return

    report.status = "running"
    report.started_at = dj_timezone.now()
    report.error = ""
    report.processed_submissions = 0
    report.total_submissions = 0
    report.stats = {
        "_progress": {
            "phase": "extracting",
            "extraction_processed": 0,
            "extraction_total": 0,
            "processing_processed": 0,
            "processing_total": 0,
            "overall_percent": 0,
        }
    }
    report.save(
        update_fields=[
            "status",
            "started_at",
            "error",
            "processed_submissions",
            "total_submissions",
            "stats",
        ]
    )

    try:
        client = CanvasClient(settings.CANVAS_URL, credential.token)

        def _on_extraction_progress(fetched_count, estimated_total):
            safe_fetched = int(max(0, fetched_count or 0))
            safe_estimated = int(max(safe_fetched, estimated_total or 0))
            _save_moderation_progress(
                report,
                phase="extracting",
                extraction_processed=safe_fetched,
                extraction_total=safe_estimated,
                processing_processed=0,
                processing_total=0,
                total_submissions=safe_fetched,
            )

        submissions = client.list_assignment_submissions(
            report.assignment.course.canvas_id,
            report.assignment.canvas_id,
            progress_callback=_on_extraction_progress,
        )
        group_set_columns, group_memberships = _build_group_set_context(client, [report.assignment])
        total_submissions = len(submissions or [])
        report.total_submissions = total_submissions
        _save_moderation_progress(
            report,
            phase="extracting",
            extraction_processed=total_submissions,
            extraction_total=total_submissions,
            processing_processed=0,
            processing_total=0,
            total_submissions=total_submissions,
        )

        scores = []
        marker_scores_raw = {}
        marker_points_raw = {}
        graded_submissions_raw = []
        processing_total = max(1, total_submissions * 2)
        processing_done = 0
        _save_moderation_progress(
            report,
            phase="processing",
            extraction_processed=total_submissions,
            extraction_total=total_submissions,
            processing_processed=0,
            processing_total=processing_total,
            processed_submissions=0,
            total_submissions=total_submissions,
        )
        processing_save_step = 5
        for idx, submission in enumerate(submissions or [], start=1):
            score = submission.get("score")
            if score is not None:
                try:
                    numeric_score = float(score)
                    scores.append(numeric_score)
                    grader_id = submission.get("grader_id")
                    marker_key = str(grader_id) if grader_id is not None else ""
                    marker_scores_raw.setdefault(marker_key, []).append(numeric_score)
                    student_id = submission.get("user_id")
                    submission_url = ""
                    if student_id:
                        submission_url = (
                            f"{settings.CANVAS_URL}/courses/{report.assignment.course.canvas_id}"
                            f"/gradebook/speed_grader?assignment_id={report.assignment.canvas_id}"
                            f"&student_id={student_id}"
                        )
                    marker_points_raw.setdefault(marker_key, []).append(
                        {
                            "submission_id": submission.get("id"),
                            "student_id": student_id,
                            "student_name": (submission.get("user") or {}).get("name") or "",
                            "score_raw": numeric_score,
                            "submission_url": submission_url,
                        }
                    )
                    graded_submissions_raw.append(
                        {
                            "submission_id": submission.get("id"),
                            "student_id": student_id,
                            "student_name": (submission.get("user") or {}).get("name") or "",
                            "grader_id": grader_id,
                            "score_raw": numeric_score,
                            "submission_url": submission_url,
                        }
                    )
                except (TypeError, ValueError):
                    pass
            processing_done = idx
            if idx % processing_save_step == 0 or idx == total_submissions:
                _save_moderation_progress(
                    report,
                    phase="processing",
                    extraction_processed=total_submissions,
                    extraction_total=total_submissions,
                    processing_processed=processing_done,
                    processing_total=processing_total,
                    processed_submissions=min(processing_done, total_submissions),
                    total_submissions=total_submissions,
                )

        stats = {}
        points_possible = report.assignment.points_possible
        try:
            points_possible = float(points_possible) if points_possible is not None else None
        except (TypeError, ValueError):
            points_possible = None

        use_percentage = bool(points_possible and points_possible > 0)
        values = []
        if use_percentage:
            values = [(score / points_possible) * 100 for score in scores]
        else:
            values = scores

        marker_scores = {}
        for marker_key, marker_values in marker_scores_raw.items():
            if use_percentage:
                marker_scores[marker_key] = [(v / points_possible) * 100 for v in marker_values]
            else:
                marker_scores[marker_key] = list(marker_values)

        marker_count = len(marker_scores)
        processing_total = max(
            1,
            total_submissions + len(graded_submissions_raw) + (marker_count * 2) + 1,
        )
        _save_moderation_progress(
            report,
            phase="processing",
            extraction_processed=total_submissions,
            extraction_total=total_submissions,
            processing_processed=processing_done,
            processing_total=processing_total,
            processed_submissions=min(processing_done, total_submissions),
            total_submissions=total_submissions,
        )

        grader_name_map = {}
        for marker_key in marker_scores.keys():
            if not marker_key:
                grader_name_map[marker_key] = "Unassigned"
                processing_done += 1
                _save_moderation_progress(
                    report,
                    phase="processing",
                    extraction_processed=total_submissions,
                    extraction_total=total_submissions,
                    processing_processed=processing_done,
                    processing_total=processing_total,
                    processed_submissions=min(processing_done, total_submissions),
                    total_submissions=total_submissions,
                )
                continue
            try:
                profile = client.get_user_profile(marker_key)
                grader_name_map[marker_key] = _format_marker_name(
                    profile.get("name") or profile.get("short_name") or f"User {marker_key}",
                    profile.get("sortable_name") or "",
                )
            except CanvasClientError:
                grader_name_map[marker_key] = _format_marker_name(f"User {marker_key}")
            processing_done += 1
            _save_moderation_progress(
                report,
                phase="processing",
                extraction_processed=total_submissions,
                extraction_total=total_submissions,
                processing_processed=processing_done,
                processing_total=processing_total,
                processed_submissions=min(processing_done, total_submissions),
                total_submissions=total_submissions,
            )

        marker_distributions = []
        for marker_key, marker_values in marker_scores.items():
            global_values = [
                v
                for other_marker_key, other_values in marker_scores.items()
                if other_marker_key != marker_key
                for v in other_values
            ]
            marker_normality = _normality_check(marker_values)
            global_normality = _normality_check(global_values)

            use_parametric = (
                marker_normality["applicable"]
                and global_normality["applicable"]
                and marker_normality["is_normal"]
                and global_normality["is_normal"]
                and len(marker_values) >= 3
                and len(global_values) >= 3
            )

            p_value = None
            test_used = "Insufficient data"
            if marker_values and global_values:
                if use_parametric:
                    test_used = "Welch t-test"
                    try:
                        p_value = float(
                            scipy_stats.ttest_ind(
                                marker_values,
                                global_values,
                                equal_var=False,
                            ).pvalue
                        )
                    except Exception:
                        p_value = None
                else:
                    test_used = "Mann-Whitney U"
                    try:
                        p_value = float(
                            scipy_stats.mannwhitneyu(
                                marker_values,
                                global_values,
                                alternative="two-sided",
                            ).pvalue
                        )
                    except Exception:
                        p_value = None
            raw_significant = bool(p_value is not None and p_value < 0.05)

            points = []
            for point in marker_points_raw.get(marker_key, []):
                score_raw = point.get("score_raw")
                if use_percentage and points_possible and points_possible > 0:
                    score_value = (float(score_raw) / points_possible) * 100
                else:
                    score_value = float(score_raw)
                points.append(
                    {
                        "submission_id": point.get("submission_id"),
                        "student_id": point.get("student_id"),
                        "student_name": point.get("student_name") or "",
                        "score": score_value,
                        "submission_url": point.get("submission_url") or "",
                    }
                )
            marker_distributions.append(
                {
                    "grader_id": marker_key,
                    "grader_name": grader_name_map.get(marker_key) or "Unknown",
                    "values": marker_values,
                    "count": len(marker_values),
                    "points": points,
                    "p_value": p_value,
                    "raw_significant": raw_significant,
                    "p_value_adjusted": None,
                    "significant": False,
                    "test_used": test_used,
                    "marker_normality_p": marker_normality["p_value"],
                    "global_normality_p": global_normality["p_value"],
                    "comparison_count": len(global_values),
                }
            )
            processing_done += 1
            _save_moderation_progress(
                report,
                phase="processing",
                extraction_processed=total_submissions,
                extraction_total=total_submissions,
                processing_processed=processing_done,
                processing_total=processing_total,
                processed_submissions=min(processing_done, total_submissions),
                total_submissions=total_submissions,
            )

        p_values_for_adjustment = [
            (idx, marker.get("p_value"))
            for idx, marker in enumerate(marker_distributions)
            if marker.get("p_value") is not None
        ]
        adjusted_p_values = _holm_bonferroni_adjust(
            [float(p) for _, p in p_values_for_adjustment]
        )
        for (idx, _), adjusted in zip(p_values_for_adjustment, adjusted_p_values):
            marker_distributions[idx]["p_value_adjusted"] = adjusted
            marker_distributions[idx]["significant"] = bool(adjusted < 0.05)
        marker_distributions.sort(key=lambda item: (item.get("grader_name") or "").casefold())
        processing_done += 1
        _save_moderation_progress(
            report,
            phase="processing",
            extraction_processed=total_submissions,
            extraction_total=total_submissions,
            processing_processed=processing_done,
            processing_total=processing_total,
            processed_submissions=min(processing_done, total_submissions),
            total_submissions=total_submissions,
        )

        tests_summary = {
            "alpha": 0.05,
            "normality_test": "Shapiro-Wilk",
            "parametric_test": "Welch t-test (two-sided)",
            "non_parametric_test": "Mann-Whitney U (two-sided)",
            "multiple_testing_correction": "Holm-Bonferroni",
            "comparison": "Each marker vs global marks excluding that marker",
            "raw_significant_markers": len([m for m in marker_distributions if m.get("raw_significant")]),
            "significant_markers": len([m for m in marker_distributions if m.get("significant")]),
            "markers_with_p_values": len(p_values_for_adjustment),
            "markers_tested": len(marker_distributions),
        }

        graded_submissions = []
        for idx, row in enumerate(graded_submissions_raw, start=1):
            marker_key = str(row.get("grader_id")) if row.get("grader_id") is not None else ""
            score_raw = row.get("score_raw")
            if use_percentage and points_possible and points_possible > 0:
                score_value = (float(score_raw) / points_possible) * 100
            else:
                score_value = float(score_raw)
            graded_submissions.append(
                {
                    "submission_id": row.get("submission_id"),
                    "student_id": row.get("student_id"),
                    "student_name": row.get("student_name") or "",
                    "grader_name": grader_name_map.get(marker_key) or "Unknown",
                    "score": score_value,
                    "submission_url": row.get("submission_url") or "",
                }
            )
            processing_done += 1
            if idx % processing_save_step == 0 or idx == len(graded_submissions_raw):
                _save_moderation_progress(
                    report,
                    phase="processing",
                    extraction_processed=total_submissions,
                    extraction_total=total_submissions,
                    processing_processed=processing_done,
                    processing_total=processing_total,
                    processed_submissions=min(processing_done, total_submissions),
                    total_submissions=total_submissions,
                )
        checked_submissions = _build_checked_submissions(
            graded_submissions=graded_submissions,
            total_submissions=total_submissions,
            report_id=report.id,
            is_percentage=use_percentage,
            fail_threshold=_get_saved_fail_threshold(report.user_id, report.assignment_id),
        )
        outstanding_by_group_sets = _build_outstanding_by_group_sets(
            submissions=submissions or [],
            course_id=report.assignment.course.canvas_id,
            group_set_columns=group_set_columns,
            group_memberships=group_memberships,
        )

        if values:
            min_score = min(values)
            max_score = max(values)
            stats = {
                "count": len(values),
                "submissions_count": total_submissions,
                "grade_range": max_score - min_score,
                "median": statistics.median(values),
                "average": statistics.mean(values),
                "min": min_score,
                "max": max_score,
                "is_percentage": use_percentage,
                "points_possible": points_possible,
                "score_values": values,
                "marker_distributions": marker_distributions,
                "graded_submissions": graded_submissions,
                "checked_submissions": checked_submissions,
                "selected_fail_threshold": _get_saved_fail_threshold(report.user_id, report.assignment_id),
                "outstanding_by_group_sets": outstanding_by_group_sets,
                "tests_summary": tests_summary,
            }
        else:
            stats = {
                "count": 0,
                "submissions_count": total_submissions,
                "grade_range": None,
                "median": None,
                "average": None,
                "min": None,
                "max": None,
                "is_percentage": use_percentage,
                "points_possible": points_possible,
                "score_values": [],
                "marker_distributions": [],
                "graded_submissions": [],
                "checked_submissions": checked_submissions,
                "selected_fail_threshold": _get_saved_fail_threshold(report.user_id, report.assignment_id),
                "outstanding_by_group_sets": outstanding_by_group_sets,
                "tests_summary": tests_summary,
            }

        report.status = "completed"
        report.completed_at = dj_timezone.now()
        report.processed_submissions = total_submissions
        report.stats = stats
        report.save(
            update_fields=["status", "completed_at", "processed_submissions", "stats"]
        )
    except CanvasClientError as exc:
        report.status = "failed"
        report.error = str(exc)
        report.completed_at = dj_timezone.now()
        report.save(update_fields=["status", "error", "completed_at"])
    except Exception as exc:
        report.status = "failed"
        report.error = str(exc)
        report.completed_at = dj_timezone.now()
        report.save(update_fields=["status", "error", "completed_at"])


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


def _default_sync_start(now):
    tz = dj_timezone.get_default_timezone()
    sept_this_year = dj_timezone.make_aware(datetime(now.year, 9, 1, 0, 0, 0), tz)
    if now < sept_this_year:
        return dj_timezone.make_aware(datetime(now.year - 1, 9, 1, 0, 0, 0), tz)
    return sept_this_year


def _assignment_on_or_after_start(assignment_data, window_start):
    unlock_at = _parse_canvas_dt((assignment_data or {}).get("unlock_at"))
    close_at = _parse_canvas_dt((assignment_data or {}).get("lock_at"))
    due_at = _parse_canvas_dt((assignment_data or {}).get("due_at"))
    for dt in (unlock_at, close_at, due_at):
        if dt and dt >= window_start:
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


def _assignment_matching_rubric_ids(assignment, criterion_name):
    target = _normalize_rubric_criterion_name(criterion_name)
    if not target:
        return []
    rubric = (assignment.raw_data or {}).get("rubric") or []
    ids = []
    for criterion in rubric:
        name = (
            criterion.get("description")
            or criterion.get("long_description")
            or criterion.get("criterion")
            or ""
        ).strip()
        if name and _normalize_rubric_criterion_name(name) == target:
            criterion_id = criterion.get("id")
            if criterion_id is not None:
                ids.append(str(criterion_id))
    return ids


def _assignment_rubric_rating_label_maps(assignment, criterion_name):
    target = _normalize_rubric_criterion_name(criterion_name)
    if not target:
        return {}
    rubric = (assignment.raw_data or {}).get("rubric") or []
    maps = {}
    for criterion in rubric:
        name = (
            criterion.get("description")
            or criterion.get("long_description")
            or criterion.get("criterion")
            or ""
        ).strip()
        if not name or _normalize_rubric_criterion_name(name) != target:
            continue
        criterion_id = criterion.get("id")
        if criterion_id is None:
            continue
        rating_map = {}
        for rating in criterion.get("ratings") or []:
            rating_id = rating.get("id")
            if rating_id is None:
                continue
            label = (
                rating.get("description")
                or rating.get("long_description")
                or rating.get("criterion")
                or ""
            ).strip()
            rating_map[str(rating_id)] = label
        maps[str(criterion_id)] = rating_map
    return maps


def _format_marker_name(name, sortable_name=""):
    sortable = (sortable_name or "").strip()
    if sortable:
        return sortable
    raw = (name or "").strip()
    if not raw:
        return "Unknown"
    parts = raw.split()
    if len(parts) < 2:
        return raw
    return f"{parts[-1]}, {' '.join(parts[:-1])}"


def _normality_check(values):
    if len(values) < 3:
        return {"applicable": False, "p_value": None, "is_normal": False}
    sample = values[:5000]
    try:
        p_value = float(scipy_stats.shapiro(sample).pvalue)
    except Exception:
        return {"applicable": False, "p_value": None, "is_normal": False}
    return {
        "applicable": True,
        "p_value": p_value,
        "is_normal": p_value >= 0.05,
    }


def _build_checked_submissions(
    graded_submissions,
    total_submissions,
    report_id,
    is_percentage,
    fail_threshold=40.0,
):
    all_rows = list(graded_submissions or [])
    if not all_rows:
        return []

    max_pass_sample = int(math.ceil(max(int(total_submissions or 0), len(all_rows)) * 0.10))
    fail_threshold = _normalize_fail_threshold(fail_threshold)
    boundaries = [50.0, 60.0, 70.0]
    boundary_tolerance = 2.0

    def _score(row):
        try:
            return float(row.get("score"))
        except (TypeError, ValueError):
            return None

    def _is_failed(score_value):
        return score_value is not None and score_value < fail_threshold

    def _is_boundary(score_value):
        if score_value is None:
            return False
        if not is_percentage:
            return False
        return any(abs(score_value - boundary) <= boundary_tolerance for boundary in boundaries)

    fails = []
    passes = []
    for row in all_rows:
        score_value = _score(row)
        if _is_failed(score_value):
            fails.append({**row, "selection_reason": "fail"})
        else:
            passes.append(row)

    boundary_passes = []
    boundary_keys = set()
    for row in passes:
        score_value = _score(row)
        if not _is_boundary(score_value):
            continue
        key = row.get("submission_id") or row.get("student_id") or f"idx:{len(boundary_passes)}"
        boundary_keys.add(str(key))
        nearest_boundary_gap = min(abs(score_value - boundary) for boundary in boundaries) if score_value is not None else 999.0
        boundary_passes.append({**row, "selection_reason": "boundary", "_boundary_gap": nearest_boundary_gap})

    remaining_pool = []
    for row in passes:
        key = row.get("submission_id") or row.get("student_id")
        key_str = str(key) if key is not None else ""
        if key_str and key_str in boundary_keys:
            continue
        remaining_pool.append(row)

    selected_boundary = []
    if len(boundary_passes) <= max_pass_sample:
        selected_boundary = list(boundary_passes)
    else:
        # Boundary passes are part of the 10% pass sample cap.
        selected_boundary = sorted(
            boundary_passes,
            key=lambda row: (
                float(row.get("_boundary_gap") or 999.0),
                (row.get("student_name") or "").casefold(),
            ),
        )[:max_pass_sample]

    random_target = max(0, max_pass_sample - len(selected_boundary))
    random_rows = []
    if random_target > 0 and remaining_pool:
        rng = random.Random(int(report_id or 0))
        if len(remaining_pool) <= random_target:
            random_rows = list(remaining_pool)
        else:
            random_rows = rng.sample(remaining_pool, random_target)
        random_rows = [{**row, "selection_reason": "random_pass"} for row in random_rows]

    selected = []
    selected.extend(fails)
    selected.extend(selected_boundary)
    selected.extend(random_rows)

    # Deduplicate by submission_id first, then by student_id.
    seen = set()
    deduped = []
    for row in selected:
        key = row.get("submission_id")
        if key is None:
            key = f"student:{row.get('student_id')}"
        key = str(key)
        if key in seen:
            continue
        seen.add(key)
        cleaned = dict(row)
        cleaned.pop("_boundary_gap", None)
        deduped.append(cleaned)

    # Keep failed first, then boundary, then random; tie-break by student name.
    order = {"fail": 0, "boundary": 1, "random_pass": 2}
    deduped.sort(
        key=lambda row: (
            order.get(row.get("selection_reason") or "", 9),
            (row.get("student_name") or "").casefold(),
        )
    )
    return deduped


def _get_saved_fail_threshold(user_id, assignment_id):
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
    # Keep compatibility with older saved values by mapping to nearest allowed threshold.
    return 50.0 if numeric >= 45.0 else 40.0


def _build_outstanding_by_group_sets(submissions, course_id, group_set_columns, group_memberships):
    if not group_set_columns:
        return []

    set_counters = {}
    for group_set_name in group_set_columns:
        set_counters[group_set_name] = defaultdict(
            lambda: {
                "group_name": "",
                "total": 0,
                "submitted": 0,
                "graded": 0,
                "outstanding": 0,
            }
        )

    for submission in submissions or []:
        user_id = submission.get("user_id")
        try:
            user_id_int = int(user_id) if user_id is not None else None
        except (TypeError, ValueError):
            user_id_int = None
        if user_id_int is None:
            continue

        score = submission.get("score")
        excused = bool(submission.get("excused"))
        submitted_at = submission.get("submitted_at")
        workflow_state = (submission.get("workflow_state") or "").strip().lower()
        is_submitted = bool(submitted_at) or workflow_state in {"submitted", "pending_review", "graded"}
        is_graded = score is not None
        is_outstanding = is_submitted and (not is_graded) and (not excused)

        for group_set_name in group_set_columns:
            group_names = sorted(
                group_memberships.get((course_id, user_id_int, group_set_name), set())
            )
            if not group_names:
                group_names = ["(No group)"]
            for group_name in group_names:
                row = set_counters[group_set_name][group_name]
                row["group_name"] = group_name
                row["total"] += 1
                if is_submitted:
                    row["submitted"] += 1
                if is_graded:
                    row["graded"] += 1
                if is_outstanding:
                    row["outstanding"] += 1

    output = []
    for group_set_name in group_set_columns:
        groups = list(set_counters[group_set_name].values())
        groups.sort(key=lambda item: (item.get("group_name") or "").casefold())
        output.append(
            {
                "group_set_name": group_set_name,
                "groups": groups,
                "outstanding_total": sum(int(g.get("outstanding") or 0) for g in groups),
                "submitted_total": sum(int(g.get("submitted") or 0) for g in groups),
                "graded_total": sum(int(g.get("graded") or 0) for g in groups),
                "total": sum(int(g.get("total") or 0) for g in groups),
            }
        )
    return output


def _phase_percent(processed, total):
    if total <= 0:
        return 0
    safe_processed = max(0, min(int(processed), int(total)))
    return int((safe_processed / int(total)) * 100)


def _overall_progress_percent(extraction_processed, extraction_total, processing_processed, processing_total):
    extraction_percent = _phase_percent(extraction_processed, extraction_total)
    processing_percent = _phase_percent(processing_processed, processing_total)
    return int((extraction_percent + processing_percent) / 2)


def _save_moderation_progress(
    report,
    *,
    phase,
    extraction_processed,
    extraction_total,
    processing_processed,
    processing_total,
    processed_submissions=None,
    total_submissions=None,
):
    progress = {
        "phase": phase,
        "extraction_processed": int(max(0, extraction_processed)),
        "extraction_total": int(max(0, extraction_total)),
        "processing_processed": int(max(0, processing_processed)),
        "processing_total": int(max(0, processing_total)),
    }
    progress["overall_percent"] = _overall_progress_percent(
        progress["extraction_processed"],
        progress["extraction_total"],
        progress["processing_processed"],
        progress["processing_total"],
    )
    current_stats = dict(report.stats or {})
    current_stats["_progress"] = progress
    report.stats = current_stats

    update_fields = ["stats"]
    if processed_submissions is not None:
        report.processed_submissions = int(max(0, processed_submissions))
        update_fields.append("processed_submissions")
    if total_submissions is not None:
        report.total_submissions = int(max(0, total_submissions))
        update_fields.append("total_submissions")
    report.save(update_fields=update_fields)


def _holm_bonferroni_adjust(p_values):
    values = list(p_values or [])
    m = len(values)
    if m == 0:
        return []
    indexed = sorted(enumerate(values), key=lambda x: x[1])
    adjusted_sorted = [0.0] * m
    running_max = 0.0
    for rank, (_, p_value) in enumerate(indexed, start=1):
        adjusted = (m - rank + 1) * p_value
        if adjusted < running_max:
            adjusted = running_max
        running_max = adjusted
        adjusted_sorted[rank - 1] = min(1.0, adjusted)
    out = [0.0] * m
    for sorted_idx, (orig_idx, _) in enumerate(indexed):
        out[orig_idx] = adjusted_sorted[sorted_idx]
    return out


def _filtered_assignments_for_report(user_id, filters):
    isolated_assignment_id_raw = (filters.get("isolated_assignment_id") or "").strip()
    course_id = (filters.get("course") or "").strip()
    course_name = (filters.get("course_name") or "").strip()
    assignment_type = (filters.get("assignment_type") or "").strip()
    rubric_criterion = (filters.get("rubric_criterion") or "").strip()
    assignment_name = (filters.get("assignment_name") or "").strip()
    enrolled_filter = (filters.get("enrolled") or "all").strip().lower()
    needs_grading_filter = (filters.get("needs_grading") or "all").strip().lower()
    date_from = _parse_report_filter_dt(filters.get("date_from"))
    date_to = _parse_report_filter_dt(filters.get("date_to"))

    assignments = CanvasAssignment.objects.select_related("course").filter(
        course__user_id=user_id,
        course__is_active=True,
        is_active=True,
        published=True,
    )

    if isolated_assignment_id_raw:
        try:
            isolated_assignment_id = int(isolated_assignment_id_raw)
        except (TypeError, ValueError):
            return []
        return list(assignments.filter(id=isolated_assignment_id).order_by("due_at", "name"))

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
    assignments = list(assignments.order_by("due_at", "name"))
    if rubric_criterion:
        assignments = [a for a in assignments if _assignment_has_rubric_criterion(a, rubric_criterion)]
    return assignments


@shared_task
def sync_canvas_for_user(user_id, existing_only=False):
    # Keep the deprecated arg for backward compatibility with queued tasks.
    _ = existing_only
    credential = CanvasCredential.objects.filter(user_id=user_id).first()
    if not credential or not credential.token:
        return

    client = CanvasClient(settings.CANVAS_URL, credential.token)
    credential.refresh_from_db(fields=["sync_stop_requested"])
    if credential.sync_stop_requested:
        credential.sync_status = "stopped"
        credential.sync_current_course_name = ""
        credential.sync_progress_note = "Sync stopped by user."
        credential.sync_stop_requested = False
        credential.save(
            update_fields=[
                "sync_status",
                "sync_current_course_name",
                "sync_progress_note",
                "sync_stop_requested",
                "updated_at",
            ]
        )
        return

    credential.sync_status = "running"
    credential.last_error = ""
    credential.sync_total_courses = 0
    credential.sync_processed_courses = 0
    credential.sync_current_course_name = ""
    credential.sync_progress_note = ""
    credential.save(
        update_fields=[
            "sync_status",
            "last_error",
            "sync_total_courses",
            "sync_processed_courses",
            "sync_current_course_name",
            "sync_progress_note",
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
        now = dj_timezone.now()
        window_start = credential.sync_start_at or _default_sync_start(now)
        seen_course_ids = set()
        eligible_courses = []

        for course_data in courses:
            canvas_course_id = course_data.get("id")
            if not canvas_course_id:
                continue
            eligible_courses.append(course_data)

        existing_course_ids = set(
            CanvasCourse.objects.filter(user_id=user_id, canvas_id__in=[c.get("id") for c in eligible_courses])
            .values_list("canvas_id", flat=True)
        )
        existing_courses = [c for c in eligible_courses if c.get("id") in existing_course_ids]
        new_courses = [c for c in eligible_courses if c.get("id") not in existing_course_ids]
        eligible_courses = existing_courses + new_courses
        first_new_course_index = len(existing_courses) + 1 if new_courses else None

        credential.sync_total_courses = len(eligible_courses)
        credential.sync_processed_courses = 0
        credential.sync_current_course_name = ""
        credential.sync_progress_note = ""
        credential.save(
            update_fields=[
                "sync_total_courses",
                "sync_processed_courses",
                "sync_current_course_name",
                "sync_progress_note",
                "updated_at",
            ]
        )

        for idx, course_data in enumerate(eligible_courses, start=1):
            credential.refresh_from_db(fields=["sync_stop_requested"])
            if credential.sync_stop_requested:
                credential.sync_status = "stopped"
                credential.sync_current_course_name = ""
                credential.sync_progress_note = "Sync stopped by user."
                credential.sync_stop_requested = False
                credential.save(
                    update_fields=[
                        "sync_status",
                        "sync_current_course_name",
                        "sync_progress_note",
                        "sync_stop_requested",
                        "updated_at",
                    ]
                )
                return

            if first_new_course_index and idx == first_new_course_index:
                credential.sync_progress_note = "Now syncing courses not yet in the database."
                credential.save(update_fields=["sync_progress_note", "updated_at"])

            canvas_course_id = course_data.get("id")
            credential.sync_current_course_name = (course_data.get("name") or f"Course {canvas_course_id}")[:255]
            credential.save(update_fields=["sync_current_course_name", "updated_at"])
            assignments = client.list_course_assignments(canvas_course_id)
            qualifying_assignments = []
            for assignment_data in assignments:
                if not assignment_data.get("published", False):
                    continue
                if _assignment_on_or_after_start(assignment_data, window_start):
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
        credential.sync_progress_note = ""
        credential.save(
            update_fields=[
                "sync_status",
                "last_sync_at",
                "last_error",
                "sync_current_course_name",
                "sync_progress_note",
                "updated_at",
            ]
        )
    except CanvasClientError as exc:
        credential.sync_status = "error"
        credential.last_error = str(exc)
        credential.sync_current_course_name = ""
        credential.sync_progress_note = ""
        credential.save(
            update_fields=[
                "sync_status",
                "last_error",
                "sync_current_course_name",
                "sync_progress_note",
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
    assignments = _filtered_assignments_for_report(report.user_id, report.filters)
    report.total_assignments = len(assignments)
    report.save(update_fields=["total_assignments"])
    selected_rubric_criterion = (report.filters or {}).get("rubric_criterion", "").strip()

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
    if selected_rubric_criterion:
        header.append(f"rubric: {selected_rubric_criterion} (value)")
        header.append(f"rubric: {selected_rubric_criterion} (label)")
    header.extend(group_set_columns)
    writer.writerow(header)
    row_count = 0

    try:
        for idx, assignment in enumerate(assignments, start=1):
            report.refresh_from_db(fields=["cancel_requested", "status"])
            if report.cancel_requested or report.status == "cancelled":
                report.status = "cancelled"
                report.completed_at = dj_timezone.now()
                report.current_assignment_name = ""
                report.save(update_fields=["status", "completed_at", "current_assignment_name"])
                return

            report.current_assignment_name = assignment.name[:255]
            report.processed_assignments = idx - 1
            report.save(update_fields=["current_assignment_name", "processed_assignments"])

            submissions = client.list_assignment_submissions(assignment.course.canvas_id, assignment.canvas_id)
            selected_rubric_values_for_assignment = []
            selected_rubric_rating_label_maps_for_assignment = {}
            if selected_rubric_criterion:
                selected_rubric_values_for_assignment = _assignment_matching_rubric_ids(
                    assignment, selected_rubric_criterion
                )
                selected_rubric_rating_label_maps_for_assignment = _assignment_rubric_rating_label_maps(
                    assignment, selected_rubric_criterion
                )
            for submission_idx, submission in enumerate(submissions or [], start=1):
                if submission_idx % 25 == 0:
                    report.refresh_from_db(fields=["cancel_requested", "status"])
                    if report.cancel_requested or report.status == "cancelled":
                        report.status = "cancelled"
                        report.completed_at = dj_timezone.now()
                        report.current_assignment_name = ""
                        report.save(
                            update_fields=[
                                "status",
                                "completed_at",
                                "current_assignment_name",
                            ]
                        )
                        return
                user = submission.get("user") or {}
                user_id = submission.get("user_id")
                try:
                    user_id_int = int(user_id) if user_id is not None else None
                except (TypeError, ValueError):
                    user_id_int = None
                rubric_value = ""
                rubric_label = ""
                if selected_rubric_values_for_assignment:
                    rubric_assessment = submission.get("rubric_assessment") or {}
                    points_values = []
                    label_values = []
                    for criterion_id in selected_rubric_values_for_assignment:
                        criterion_result = rubric_assessment.get(criterion_id) or {}
                        points = criterion_result.get("points")
                        if points is not None:
                            points_values.append(str(points))
                        rating_id = criterion_result.get("rating_id")
                        if rating_id is not None:
                            rating_map = selected_rubric_rating_label_maps_for_assignment.get(
                                str(criterion_id), {}
                            )
                            label = rating_map.get(str(rating_id), "")
                            if label:
                                label_values.append(label)
                    rubric_value = "; ".join(points_values)
                    rubric_label = "; ".join(label_values)
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
                        *([rubric_value, rubric_label] if selected_rubric_criterion else []),
                        *group_values,
                    ],
                )
                row_count += 1

            report.processed_assignments = idx
            report.save(update_fields=["processed_assignments"])

        report.refresh_from_db(fields=["cancel_requested", "status"])
        if report.cancel_requested or report.status == "cancelled":
            report.status = "cancelled"
            report.completed_at = dj_timezone.now()
            report.current_assignment_name = ""
            report.save(update_fields=["status", "completed_at", "current_assignment_name"])
            return

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


def _assignment_type_label(assignment):
    submission_types = list((assignment.submission_types or []))
    normalized = [str(value).strip() for value in submission_types if str(value).strip()]
    if not normalized:
        return "unknown"
    return ", ".join(normalized)


@shared_task
def generate_staff_marking_report(report_id):
    report = CanvasStaffMarkingReport.objects.select_related("user").filter(id=report_id).first()
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
    assignments = _filtered_assignments_for_report(report.user_id, report.filters)
    report.total_assignments = len(assignments)
    report.save(update_fields=["total_assignments"])

    grader_name_cache = {}
    counts_by_grader = defaultdict(lambda: defaultdict(int))
    assignment_types = set()

    try:
        for idx, assignment in enumerate(assignments, start=1):
            report.refresh_from_db(fields=["cancel_requested", "status"])
            if report.cancel_requested or report.status == "cancelled":
                report.status = "cancelled"
                report.completed_at = dj_timezone.now()
                report.current_assignment_name = ""
                report.save(update_fields=["status", "completed_at", "current_assignment_name"])
                return

            report.current_assignment_name = assignment.name[:255]
            report.processed_assignments = idx - 1
            report.save(update_fields=["current_assignment_name", "processed_assignments"])

            type_label = _assignment_type_label(assignment)
            assignment_types.add(type_label)
            submissions = client.list_assignment_submissions(assignment.course.canvas_id, assignment.canvas_id)

            for submission_idx, submission in enumerate(submissions or [], start=1):
                if submission_idx % 25 == 0:
                    report.refresh_from_db(fields=["cancel_requested", "status"])
                    if report.cancel_requested or report.status == "cancelled":
                        report.status = "cancelled"
                        report.completed_at = dj_timezone.now()
                        report.current_assignment_name = ""
                        report.save(
                            update_fields=[
                                "status",
                                "completed_at",
                                "current_assignment_name",
                            ]
                        )
                        return
                score = submission.get("score")
                if score is None:
                    continue

                grader_id = submission.get("grader_id")
                grader_key = str(grader_id) if grader_id is not None else ""
                if grader_key not in grader_name_cache:
                    if not grader_key:
                        grader_name_cache[grader_key] = "Unassigned"
                    else:
                        try:
                            profile = client.get_user_profile(grader_key)
                            grader_name_cache[grader_key] = _format_marker_name(
                                profile.get("name") or profile.get("short_name") or f"User {grader_key}",
                                profile.get("sortable_name") or "",
                            )
                        except CanvasClientError:
                            grader_name_cache[grader_key] = _format_marker_name(f"User {grader_key}")
                counts_by_grader[grader_key][type_label] += 1

            report.processed_assignments = idx
            report.save(update_fields=["processed_assignments"])

        report.refresh_from_db(fields=["cancel_requested", "status"])
        if report.cancel_requested or report.status == "cancelled":
            report.status = "cancelled"
            report.completed_at = dj_timezone.now()
            report.current_assignment_name = ""
            report.save(update_fields=["status", "completed_at", "current_assignment_name"])
            return

        sorted_types = sorted(assignment_types, key=lambda v: v.casefold())
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["grader_id", "grader_name", *sorted_types, "total_marked_submissions"])

        row_count = 0
        sort_rows = []
        for grader_key, type_counts in counts_by_grader.items():
            grader_name = grader_name_cache.get(grader_key) or "Unknown"
            total = sum(int(type_counts.get(t) or 0) for t in sorted_types)
            sort_rows.append((grader_name, grader_key, type_counts, total))
        sort_rows.sort(key=lambda item: (item[0].casefold(), item[1]))

        for grader_name, grader_key, type_counts, total in sort_rows:
            writer.writerow(
                [
                    grader_key,
                    grader_name,
                    *[int(type_counts.get(t) or 0) for t in sorted_types],
                    total,
                ]
            )
            row_count += 1

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
