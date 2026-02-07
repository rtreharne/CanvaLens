from django.db import migrations, models
from django.contrib.postgres.search import SearchVectorField
from django.contrib.postgres.indexes import GinIndex
import django.db.models.deletion
import pgvector.django


class Migration(migrations.Migration):
    initial = True

    dependencies = []

    operations = [
        migrations.RunSQL("CREATE EXTENSION IF NOT EXISTS vector"),
        migrations.CreateModel(
            name="StaffProfile",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("profile_url", models.URLField(unique=True)),
                ("name", models.CharField(blank=True, max_length=255)),
                ("title", models.CharField(blank=True, max_length=64)),
                ("suffix", models.CharField(blank=True, max_length=128)),
                ("faculty", models.CharField(blank=True, max_length=255)),
                ("institute", models.CharField(blank=True, max_length=255)),
                ("department", models.CharField(blank=True, max_length=255)),
                ("text_content", models.TextField(blank=True)),
                ("raw_html", models.TextField(blank=True)),
                ("etag", models.CharField(blank=True, max_length=255)),
                ("last_modified", models.CharField(blank=True, max_length=255)),
                ("content_hash", models.CharField(blank=True, max_length=64)),
                ("last_fetched_at", models.DateTimeField(blank=True, null=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
        ),
        migrations.CreateModel(
            name="CrawlUrl",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("url", models.URLField(unique=True)),
                ("depth", models.IntegerField(default=0)),
                ("status", models.CharField(choices=[("queued", "Queued"), ("fetched", "Fetched"), ("skipped", "Skipped"), ("error", "Error")], default="queued", max_length=16)),
                ("http_status", models.IntegerField(blank=True, null=True)),
                ("etag", models.CharField(blank=True, max_length=255)),
                ("last_modified", models.CharField(blank=True, max_length=255)),
                ("content_hash", models.CharField(blank=True, max_length=64)),
                ("last_fetched_at", models.DateTimeField(blank=True, null=True)),
                ("error", models.TextField(blank=True)),
            ],
        ),
        migrations.CreateModel(
            name="Chunk",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("chunk_index", models.IntegerField()),
                ("chunk_text", models.TextField()),
                ("embedding", pgvector.django.VectorField(dimensions=1536)),
                ("tsv", SearchVectorField(null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("staff", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="chunks", to="directory.staffprofile")),
            ],
        ),
        migrations.AddIndex(
            model_name="chunk",
            index=GinIndex(fields=["tsv"], name="directory_c_tsv_gin"),
        ),
        migrations.AddIndex(
            model_name="chunk",
            index=pgvector.django.HnswIndex(fields=["embedding"], m=16, ef_construction=64, opclasses=["vector_cosine_ops"], name="chunk_embedding_hnsw"),
        ),
    ]
