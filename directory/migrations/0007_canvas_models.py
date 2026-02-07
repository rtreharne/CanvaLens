from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("directory", "0006_search_chat_log"),
        ("auth", "0012_alter_user_first_name_max_length"),
    ]

    operations = [
        migrations.CreateModel(
            name="CanvasCredential",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("token", models.CharField(blank=True, max_length=255)),
                ("token_last_validated_at", models.DateTimeField(blank=True, null=True)),
                ("sync_status", models.CharField(default="never", max_length=32)),
                ("last_sync_at", models.DateTimeField(blank=True, null=True)),
                ("last_error", models.TextField(blank=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("user", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="canvas_credential", to="auth.user")),
            ],
        ),
        migrations.CreateModel(
            name="CanvasCourse",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("canvas_id", models.BigIntegerField()),
                ("name", models.CharField(max_length=255)),
                ("course_code", models.CharField(blank=True, max_length=255)),
                ("workflow_state", models.CharField(blank=True, max_length=64)),
                ("term_name", models.CharField(blank=True, max_length=255)),
                ("start_at", models.DateTimeField(blank=True, null=True)),
                ("end_at", models.DateTimeField(blank=True, null=True)),
                ("is_active", models.BooleanField(default=True)),
                ("raw_data", models.JSONField(blank=True, default=dict)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="canvas_courses", to="auth.user")),
            ],
            options={
                "ordering": ("name",),
                "unique_together": {("user", "canvas_id")},
            },
        ),
        migrations.CreateModel(
            name="CanvasAssignment",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("canvas_id", models.BigIntegerField()),
                ("name", models.CharField(max_length=255)),
                ("published", models.BooleanField(default=False)),
                ("unlock_at", models.DateTimeField(blank=True, null=True)),
                ("close_at", models.DateTimeField(blank=True, null=True)),
                ("due_at", models.DateTimeField(blank=True, null=True)),
                ("submission_types", models.JSONField(blank=True, default=list)),
                ("assignment_group_name", models.CharField(blank=True, max_length=255)),
                ("points_possible", models.FloatField(blank=True, null=True)),
                ("html_url", models.URLField(blank=True)),
                ("is_active", models.BooleanField(default=True)),
                ("raw_data", models.JSONField(blank=True, default=dict)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("course", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="assignments", to="directory.canvascourse")),
            ],
            options={
                "ordering": ("due_at", "name"),
                "unique_together": {("course", "canvas_id")},
            },
        ),
    ]
