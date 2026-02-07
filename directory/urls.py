from django.urls import path
from . import views

urlpatterns = [
    path("", views.index, name="index"),
    path("admin-dashboard/", views.admin_dashboard, name="admin_dashboard"),
    path("admin-dashboard/subaccounts/create/", views.admin_subaccount_create, name="admin_subaccount_create"),
    path(
        "admin-dashboard/subaccounts/<int:subaccount_id>/reset-password/",
        views.admin_subaccount_reset_password,
        name="admin_subaccount_reset_password",
    ),
    path(
        "admin-dashboard/subaccounts/<int:subaccount_id>/toggle-active/",
        views.admin_subaccount_toggle_active,
        name="admin_subaccount_toggle_active",
    ),
    path("canvas/settings/save/", views.canvas_settings_save, name="canvas_settings_save"),
    path("canvas/sync-source/save/", views.canvas_sync_source_save, name="canvas_sync_source_save"),
    path("canvas/sync/", views.canvas_sync, name="canvas_sync"),
    path("canvas/sync/progress/", views.canvas_sync_progress, name="canvas_sync_progress"),
    path("canvas/burn-everything/", views.canvas_burn_everything, name="canvas_burn_everything"),
    path("canvas/assignments/", views.canvas_assignments, name="canvas_assignments"),
    path("canvas/assignments/data/", views.canvas_assignments_data, name="canvas_assignments_data"),
    path(
        "canvas/assignments/<int:assignment_id>/moderate/",
        views.canvas_assignment_moderate,
        name="canvas_assignment_moderate",
    ),
    path(
        "canvas/assignments/<int:assignment_id>/moderate/regenerate/",
        views.canvas_assignment_moderate_regenerate,
        name="canvas_assignment_moderate_regenerate",
    ),
    path(
        "canvas/assignments/moderate/<int:report_id>/progress/",
        views.canvas_assignment_moderate_progress,
        name="canvas_assignment_moderate_progress",
    ),
    path(
        "canvas/assignments/moderate/<int:report_id>/delete/",
        views.canvas_assignment_moderate_delete,
        name="canvas_assignment_moderate_delete",
    ),
    path(
        "canvas/assignments/moderate/<int:report_id>/review/save/",
        views.canvas_assignment_moderate_save_review,
        name="canvas_assignment_moderate_save_review",
    ),
    path(
        "canvas/assignments/moderate/<int:report_id>/threshold/save/",
        views.canvas_assignment_moderate_save_threshold,
        name="canvas_assignment_moderate_save_threshold",
    ),
    path("canvas/reports/table/", views.canvas_reports_table, name="canvas_reports_table"),
    path("canvas/reports/create/", views.canvas_reports_create, name="canvas_reports_create"),
    path("canvas/reports/progress/", views.canvas_report_progress, name="canvas_report_progress"),
    path("canvas/reports/<int:report_id>/cancel/", views.canvas_report_cancel, name="canvas_report_cancel"),
    path("canvas/reports/<int:report_id>/delete/", views.canvas_report_delete, name="canvas_report_delete"),
    path("canvas/reports/<int:report_id>/download/", views.canvas_report_download, name="canvas_report_download"),
]
