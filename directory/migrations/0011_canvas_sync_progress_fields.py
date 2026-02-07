from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("directory", "0010_canvascredential_default_term_name"),
    ]

    operations = [
        migrations.AddField(
            model_name="canvascredential",
            name="sync_current_course_name",
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AddField(
            model_name="canvascredential",
            name="sync_processed_courses",
            field=models.IntegerField(default=0),
        ),
        migrations.AddField(
            model_name="canvascredential",
            name="sync_total_courses",
            field=models.IntegerField(default=0),
        ),
    ]
