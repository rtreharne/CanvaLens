from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("directory", "0014_remove_canvascredential_default_term_name"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="canvascredential",
            name="include_admin_courses",
        ),
    ]
