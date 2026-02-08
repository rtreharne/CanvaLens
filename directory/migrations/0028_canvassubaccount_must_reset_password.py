from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("directory", "0027_canvascredential_sync_stop_requested_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="canvassubaccount",
            name="must_reset_password",
            field=models.BooleanField(default=False),
        ),
    ]
