from django.contrib import admin
from .models import (
    CanvasCredential,
    CanvasCourse,
    CanvasAssignment,
)


@admin.register(CanvasCredential)
class CanvasCredentialAdmin(admin.ModelAdmin):
    list_display = ("user", "sync_status", "token_last_validated_at", "last_sync_at", "updated_at")
    search_fields = ("user__username", "user__email")


@admin.register(CanvasCourse)
class CanvasCourseAdmin(admin.ModelAdmin):
    list_display = ("name", "user", "workflow_state", "term_name", "is_active", "updated_at")
    search_fields = ("name", "course_code", "user__username")
    list_filter = ("is_active", "workflow_state")


@admin.register(CanvasAssignment)
class CanvasAssignmentAdmin(admin.ModelAdmin):
    list_display = ("name", "course", "published", "unlock_at", "close_at", "due_at", "is_active")
    search_fields = ("name", "course__name", "course__user__username")
    list_filter = ("published", "is_active")
