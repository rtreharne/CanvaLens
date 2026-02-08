from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("directory", "0026_canvasstaffmarkingreport"),
    ]

    operations = [
        migrations.AddField(
            model_name="canvascredential",
            name="sync_progress_note",
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AddField(
            model_name="canvascredential",
            name="sync_stop_requested",
            field=models.BooleanField(default=False),
        ),
    ]
