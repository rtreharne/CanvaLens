from django.db import migrations, models
import django.db.models.deletion


def forwards(apps, schema_editor):
    StaffProfile = apps.get_model("directory", "StaffProfile")
    Faculty = apps.get_model("directory", "Faculty")
    Institute = apps.get_model("directory", "Institute")
    Department = apps.get_model("directory", "Department")

    for staff in StaffProfile.objects.all():
        faculty_name = (staff.faculty_text or "").strip()
        institute_name = (staff.institute_text or "").strip()
        department_name = (staff.department_text or "").strip()

        faculty = None
        institute = None
        department = None

        if faculty_name:
            faculty, _ = Faculty.objects.get_or_create(name=faculty_name)
        if institute_name:
            institute, _ = Institute.objects.get_or_create(
                name=institute_name,
                defaults={"faculty": faculty},
            )
            if faculty and institute.faculty_id != faculty.id:
                institute.faculty_id = faculty.id
                institute.save(update_fields=["faculty"])
        if department_name:
            department, _ = Department.objects.get_or_create(
                name=department_name,
                defaults={"institute": institute},
            )
            if institute and department.institute_id != institute.id:
                department.institute_id = institute.id
                department.save(update_fields=["institute"])

        staff.faculty_id = faculty.id if faculty else None
        staff.institute_id = institute.id if institute else None
        staff.department_id = department.id if department else None
        staff.save(update_fields=["faculty", "institute", "department"])


def backwards(apps, schema_editor):
    StaffProfile = apps.get_model("directory", "StaffProfile")
    for staff in StaffProfile.objects.all():
        staff.faculty_text = staff.faculty.name if staff.faculty else ""
        staff.institute_text = staff.institute.name if staff.institute else ""
        staff.department_text = staff.department.name if staff.department else ""
        staff.save(update_fields=["faculty_text", "institute_text", "department_text"])


class Migration(migrations.Migration):
    dependencies = [
        ("directory", "0004_crawlcontrol"),
    ]

    operations = [
        migrations.CreateModel(
            name="Faculty",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=255, unique=True)),
            ],
        ),
        migrations.CreateModel(
            name="Institute",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=255, unique=True)),
                ("faculty", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="institutes", to="directory.faculty")),
            ],
        ),
        migrations.CreateModel(
            name="Department",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=255, unique=True)),
                ("institute", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="departments", to="directory.institute")),
            ],
        ),
        migrations.RenameField(
            model_name="staffprofile",
            old_name="faculty",
            new_name="faculty_text",
        ),
        migrations.RenameField(
            model_name="staffprofile",
            old_name="institute",
            new_name="institute_text",
        ),
        migrations.RenameField(
            model_name="staffprofile",
            old_name="department",
            new_name="department_text",
        ),
        migrations.AddField(
            model_name="staffprofile",
            name="faculty",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to="directory.faculty"),
        ),
        migrations.AddField(
            model_name="staffprofile",
            name="institute",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to="directory.institute"),
        ),
        migrations.AddField(
            model_name="staffprofile",
            name="department",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to="directory.department"),
        ),
        migrations.RunPython(forwards, backwards),
    ]
