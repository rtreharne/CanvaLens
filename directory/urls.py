from django.urls import path
from . import views

urlpatterns = [
    path("", views.index, name="index"),
    path("admin-dashboard/", views.admin_dashboard, name="admin_dashboard"),
    path("canvas/settings/save/", views.canvas_settings_save, name="canvas_settings_save"),
    path("canvas/sync-source/save/", views.canvas_sync_source_save, name="canvas_sync_source_save"),
    path("canvas/sync/", views.canvas_sync, name="canvas_sync"),
    path("canvas/sync/progress/", views.canvas_sync_progress, name="canvas_sync_progress"),
    path("canvas/burn-everything/", views.canvas_burn_everything, name="canvas_burn_everything"),
    path("canvas/assignments/", views.canvas_assignments, name="canvas_assignments"),
    path("canvas/assignments/data/", views.canvas_assignments_data, name="canvas_assignments_data"),
    path("canvas/reports/table/", views.canvas_reports_table, name="canvas_reports_table"),
    path("canvas/reports/create/", views.canvas_reports_create, name="canvas_reports_create"),
    path("canvas/reports/progress/", views.canvas_report_progress, name="canvas_report_progress"),
    path("canvas/reports/<int:report_id>/cancel/", views.canvas_report_cancel, name="canvas_report_cancel"),
    path("canvas/reports/<int:report_id>/delete/", views.canvas_report_delete, name="canvas_report_delete"),
    path("canvas/reports/<int:report_id>/download/", views.canvas_report_download, name="canvas_report_download"),
]
