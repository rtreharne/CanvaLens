from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("directory", "0024_canvascredential_sync_start_at"),
    ]

    operations = [
        migrations.AddField(
            model_name="canvascredential",
            name="subaccounts_maintenance_mode",
            field=models.BooleanField(default=False),
        ),
    ]
