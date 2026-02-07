from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("directory", "0011_canvas_sync_progress_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="canvascredential",
            name="include_admin_courses",
            field=models.BooleanField(default=False),
        ),
    ]
