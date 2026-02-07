from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("directory", "0023_remove_canvassubaccount_last_issued_password_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="canvascredential",
            name="sync_start_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
