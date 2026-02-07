from django.db import models
from django.contrib.auth.models import User


class SeedUrl(models.Model):
    url = models.URLField(unique=True)
    priority = models.IntegerField(default=10)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.url


class CrawlControl(models.Model):
    is_paused = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"paused={self.is_paused}"


class SearchLog(models.Model):
    query = models.TextField(blank=True)
    filters = models.JSONField(default=dict, blank=True)
    offset = models.IntegerField(default=0)
    limit = models.IntegerField(default=0)
    results_count = models.IntegerField(default=0)
    user = models.ForeignKey("auth.User", null=True, blank=True, on_delete=models.SET_NULL)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True)
    request_meta = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.created_at:%Y-%m-%d %H:%M:%S} search"


class ChatLog(models.Model):
    question = models.TextField(blank=True)
    filters = models.JSONField(default=dict, blank=True)
    response = models.JSONField(default=dict, blank=True)
    sources = models.JSONField(default=list, blank=True)
    user = models.ForeignKey("auth.User", null=True, blank=True, on_delete=models.SET_NULL)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True)
    request_meta = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.created_at:%Y-%m-%d %H:%M:%S} chat"


class CanvasCredential(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="canvas_credential")
    token = models.CharField(max_length=255, blank=True)
    sync_source = models.CharField(max_length=32, default="enrolled")
    sync_start_at = models.DateTimeField(null=True, blank=True)
    subaccounts_maintenance_mode = models.BooleanField(default=False)
    admin_account_id = models.BigIntegerField(null=True, blank=True)
    admin_account_name = models.CharField(max_length=255, blank=True)
    token_last_validated_at = models.DateTimeField(null=True, blank=True)
    sync_status = models.CharField(max_length=32, default="never")
    sync_total_courses = models.IntegerField(default=0)
    sync_processed_courses = models.IntegerField(default=0)
    sync_current_course_name = models.CharField(max_length=255, blank=True)
    last_sync_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Canvas credential for {self.user.username}"


class CanvasCourse(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="canvas_courses")
    canvas_id = models.BigIntegerField()
    name = models.CharField(max_length=255)
    is_enrolled = models.BooleanField(default=False)
    course_code = models.CharField(max_length=255, blank=True)
    workflow_state = models.CharField(max_length=64, blank=True)
    term_name = models.CharField(max_length=255, blank=True)
    start_at = models.DateTimeField(null=True, blank=True)
    end_at = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    raw_data = models.JSONField(default=dict, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("user", "canvas_id")
        ordering = ("name",)

    def __str__(self):
        return f"{self.name} ({self.user.username})"


class CanvasAssignment(models.Model):
    course = models.ForeignKey(CanvasCourse, on_delete=models.CASCADE, related_name="assignments")
    canvas_id = models.BigIntegerField()
    name = models.CharField(max_length=255)
    published = models.BooleanField(default=False)
    unlock_at = models.DateTimeField(null=True, blank=True)
    close_at = models.DateTimeField(null=True, blank=True)
    due_at = models.DateTimeField(null=True, blank=True)
    submission_types = models.JSONField(default=list, blank=True)
    assignment_group_name = models.CharField(max_length=255, blank=True)
    points_possible = models.FloatField(null=True, blank=True)
    html_url = models.URLField(blank=True)
    is_active = models.BooleanField(default=True)
    raw_data = models.JSONField(default=dict, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("course", "canvas_id")
        ordering = ("due_at", "name")

    def __str__(self):
        return f"{self.name} ({self.course.name})"


class CanvasSubmissionReport(models.Model):
    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("running", "Running"),
        ("completed", "Completed"),
        ("cancelled", "Cancelled"),
        ("failed", "Failed"),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="canvas_submission_reports")
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default="pending")
    filters = models.JSONField(default=dict, blank=True)
    total_assignments = models.IntegerField(default=0)
    processed_assignments = models.IntegerField(default=0)
    current_assignment_name = models.CharField(max_length=255, blank=True)
    row_count = models.IntegerField(default=0)
    csv_content = models.TextField(blank=True)
    cancel_requested = models.BooleanField(default=False)
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-created_at",)

    def __str__(self):
        return f"Report {self.id} ({self.status})"


class CanvasAssignmentModerationReport(models.Model):
    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("running", "Running"),
        ("completed", "Completed"),
        ("failed", "Failed"),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="canvas_assignment_moderation_reports")
    assignment = models.ForeignKey(
        CanvasAssignment, on_delete=models.CASCADE, related_name="moderation_reports"
    )
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default="pending")
    total_submissions = models.IntegerField(default=0)
    processed_submissions = models.IntegerField(default=0)
    stats = models.JSONField(default=dict, blank=True)
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-created_at",)

    def __str__(self):
        return f"Moderation {self.id} ({self.status})"


class CanvasModerationSubmissionReview(models.Model):
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="canvas_moderation_submission_reviews"
    )
    assignment = models.ForeignKey(
        CanvasAssignment, on_delete=models.CASCADE, related_name="moderation_submission_reviews"
    )
    report = models.ForeignKey(
        CanvasAssignmentModerationReport,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="submission_reviews",
    )
    submission_id = models.BigIntegerField()
    student_id = models.BigIntegerField(null=True, blank=True)
    student_name = models.CharField(max_length=255, blank=True)
    grader_name = models.CharField(max_length=255, blank=True)
    score = models.FloatField(null=True, blank=True)
    notes = models.TextField(blank=True)
    is_checked = models.BooleanField(default=False)
    has_issue = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("user", "assignment", "submission_id")
        ordering = ("-updated_at",)

    def __str__(self):
        return f"Moderation review {self.assignment_id}:{self.submission_id}"


class CanvasModerationAssignmentPreference(models.Model):
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="canvas_moderation_assignment_preferences"
    )
    assignment = models.ForeignKey(
        CanvasAssignment, on_delete=models.CASCADE, related_name="moderation_preferences"
    )
    fail_threshold = models.FloatField(default=40.0)
    updated_at = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("user", "assignment")
        ordering = ("-updated_at",)

    def __str__(self):
        return f"Moderation preference {self.assignment_id} ({self.fail_threshold})"


class CanvasSubAccount(models.Model):
    owner = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="canvas_subaccounts_owned"
    )
    user = models.OneToOneField(
        User, on_delete=models.CASCADE, related_name="canvas_subaccount_profile"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("user__username",)
        unique_together = ("owner", "user")

    def __str__(self):
        return f"{self.owner.username} -> {self.user.username}"
