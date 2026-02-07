from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("directory", "0012_canvascredential_include_admin_courses"),
    ]

    operations = [
        migrations.AddField(
            model_name="canvascourse",
            name="is_enrolled",
            field=models.BooleanField(default=False),
        ),
    ]
