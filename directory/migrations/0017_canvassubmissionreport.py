from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("directory", "0016_canvas_sync_source_fields"),
        ("auth", "0012_alter_user_first_name_max_length"),
    ]

    operations = [
        migrations.CreateModel(
            name="CanvasSubmissionReport",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "Pending"),
                            ("running", "Running"),
                            ("completed", "Completed"),
                            ("cancelled", "Cancelled"),
                            ("failed", "Failed"),
                        ],
                        default="pending",
                        max_length=16,
                    ),
                ),
                ("filters", models.JSONField(blank=True, default=dict)),
                ("total_assignments", models.IntegerField(default=0)),
                ("processed_assignments", models.IntegerField(default=0)),
                ("current_assignment_name", models.CharField(blank=True, max_length=255)),
                ("row_count", models.IntegerField(default=0)),
                ("csv_content", models.TextField(blank=True)),
                ("cancel_requested", models.BooleanField(default=False)),
                ("error", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="canvas_submission_reports",
                        to="auth.user",
                    ),
                ),
            ],
            options={"ordering": ("-created_at",)},
        ),
    ]
