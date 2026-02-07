from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("directory", "0013_canvascourse_is_enrolled"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="canvascredential",
            name="default_term_name",
        ),
    ]
