from __future__ import annotations

from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("ndvi", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="NdviRasterArtifact",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("owner_id", models.IntegerField(db_index=True)),
                (
                    "engine",
                    models.CharField(
                        default=settings.NDVI_RASTER_ENGINE_NAME, max_length=64
                    ),
                ),
                ("date", models.DateField()),
                ("size", models.PositiveSmallIntegerField()),
                ("max_cloud", models.PositiveSmallIntegerField()),
                (
                    "content_hash",
                    models.CharField(db_index=True, max_length=64),
                ),
                (
                    "image",
                    models.FileField(upload_to="ndvi/rasters/%Y/%m/%d/"),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("last_error", models.TextField(blank=True, null=True)),
                (
                    "farm",
                    models.ForeignKey(
                        on_delete=models.deletion.CASCADE,
                        related_name="ndvi_rasters",
                        to="farms.farm",
                    ),
                ),
            ],
            options={
                "indexes": [
                    models.Index(
                        fields=["owner_id", "date"],
                        name="ndvi_ndvir_owner_id_5f4203_idx",
                    ),
                    models.Index(
                        fields=["engine", "date"],
                        name="ndvi_ndvir_engine__a0225e_idx",
                    ),
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=["farm", "engine", "date", "size", "max_cloud"],
                        name="uniq_ndvi_raster_farm_engine_date_size_cloud",
                    )
                ],
            },
        ),
    ]
