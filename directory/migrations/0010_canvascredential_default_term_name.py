from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("directory", "0009_delete_legacy_staff_models"),
    ]

    operations = [
        migrations.AddField(
            model_name="canvascredential",
            name="default_term_name",
            field=models.CharField(blank=True, max_length=255),
        ),
    ]
