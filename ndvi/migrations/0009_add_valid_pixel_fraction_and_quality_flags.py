from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("ndvi", "0008_add_phase3_followup_hardening"),
    ]

    operations = [
        migrations.AddField(
            model_name="ndviobservation",
            name="valid_pixel_fraction",
            field=models.FloatField(
                blank=True,
                null=True,
                help_text="Fraction of pixels that passed SCL/quality masking",
            ),
        ),
        migrations.AddField(
            model_name="ndviobservation",
            name="quality_flags",
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text="Quality indicators: cloud_heavy, partial_tile, "
                "low_valid_pixel_fraction, water_detected, etc.",
            ),
        ),
    ]
