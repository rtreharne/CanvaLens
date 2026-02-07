from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("directory", "0008_rename_directory_c_tsv_gin_directory_c_tsv_29df4d_gin"),
    ]

    operations = [
        migrations.DeleteModel(
            name="Chunk",
        ),
        migrations.DeleteModel(
            name="CrawlUrl",
        ),
        migrations.DeleteModel(
            name="StaffProfile",
        ),
        migrations.DeleteModel(
            name="Department",
        ),
        migrations.DeleteModel(
            name="Institute",
        ),
        migrations.DeleteModel(
            name="Faculty",
        ),
    ]
