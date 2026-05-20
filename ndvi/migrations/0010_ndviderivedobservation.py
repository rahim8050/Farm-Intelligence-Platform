import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("farms", "0001_initial"),
        ("ndvi", "0009_add_valid_pixel_fraction_and_quality_flags"),
    ]

    operations = [
        migrations.CreateModel(
            name="NdviDerivedObservation",
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
                ("engine", models.CharField(max_length=64)),
                ("bucket_date", models.DateField()),
                (
                    "source",
                    models.CharField(
                        help_text=(
                            "Source engine that produced this V2 observation"
                        ),
                        max_length=32,
                    ),
                ),
                (
                    "selected_ndvi",
                    models.FloatField(
                        blank=True,
                        help_text="Selected NDVI value "
                        "(may be null if quality insufficient)",
                        null=True,
                    ),
                ),
                (
                    "smoothed_ndvi",
                    models.FloatField(
                        blank=True,
                        help_text="Temporally smoothed NDVI value",
                        null=True,
                    ),
                ),
                (
                    "confidence",
                    models.FloatField(help_text="Confidence score in [0, 1]"),
                ),
                (
                    "confidence_components",
                    models.JSONField(
                        blank=True,
                        default=dict,
                        help_text="Breakdown of confidence formula components",
                    ),
                ),
                (
                    "quality_flags",
                    models.JSONField(
                        blank=True,
                        default=dict,
                        help_text="Quality flags: cloud_heavy, "
                        "low_confidence, outlier_removed, etc.",
                    ),
                ),
                (
                    "is_null",
                    models.BooleanField(
                        default=False,
                        help_text="True when this observation "
                        "was forced to null",
                    ),
                ),
                (
                    "null_reason",
                    models.CharField(
                        blank=True,
                        help_text="Reason for null output",
                        max_length=64,
                        null=True,
                    ),
                ),
                (
                    "created_at",
                    models.DateTimeField(auto_now_add=True),
                ),
                (
                    "updated_at",
                    models.DateTimeField(auto_now=True),
                ),
                (
                    "farm",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="ndvi_v2_observations",
                        to="farms.farm",
                    ),
                ),
                (
                    "v1_observation",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="v2_observation",
                        to="ndvi.ndviobservation",
                    ),
                ),
            ],
            options={
                "indexes": [
                    models.Index(
                        fields=["farm", "engine", "bucket_date"],
                        name="ndvi_ndvide_farm_id_6d3a46_idx",
                    ),
                    models.Index(
                        fields=["engine", "confidence"],
                        name="ndvi_ndvide_engine_1a5e3f_idx",
                    ),
                    models.Index(
                        fields=["source", "bucket_date"],
                        name="ndvi_ndvide_source_8b2c1d_idx",
                    ),
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("v1_observation",),
                        name="uniq_v2_per_v1_observation",
                    ),
                    models.UniqueConstraint(
                        fields=("farm", "engine", "bucket_date"),
                        name="uniq_v2_farm_engine_bucket",
                    ),
                ],
            },
        ),
    ]
