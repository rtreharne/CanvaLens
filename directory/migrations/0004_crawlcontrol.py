from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("directory", "0003_seedurl"),
    ]

    operations = [
        migrations.CreateModel(
            name="CrawlControl",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("is_paused", models.BooleanField(default=False)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
        ),
    ]
