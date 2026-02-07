from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("directory", "0002_crawlurl_priority"),
    ]

    operations = [
        migrations.CreateModel(
            name="SeedUrl",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("url", models.URLField(unique=True)),
                ("priority", models.IntegerField(default=10)),
                ("active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
        ),
    ]
