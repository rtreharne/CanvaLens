from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("directory", "0015_remove_canvascredential_include_admin_courses"),
    ]

    operations = [
        migrations.AddField(
            model_name="canvascredential",
            name="admin_account_id",
            field=models.BigIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="canvascredential",
            name="admin_account_name",
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AddField(
            model_name="canvascredential",
            name="sync_source",
            field=models.CharField(default="enrolled", max_length=32),
        ),
    ]
