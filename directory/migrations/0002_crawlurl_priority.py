from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("directory", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="crawlurl",
            name="priority",
            field=models.IntegerField(default=0),
        ),
    ]
