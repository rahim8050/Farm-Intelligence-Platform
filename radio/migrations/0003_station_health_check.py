from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("radio", "0002_add_provider_type"),
    ]

    operations = [
        migrations.AddField(
            model_name="station",
            name="is_available",
            field=models.BooleanField(
                blank=True,
                help_text=(
                    "Set by the periodic health check. None = never checked, "
                    "True = reachable, False = unreachable."
                ),
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="station",
            name="last_health_check_at",
            field=models.DateTimeField(
                blank=True,
                help_text="Timestamp of the most recent health check.",
                null=True,
            ),
        ),
        migrations.AddIndex(
            model_name="station",
            index=models.Index(
                fields=["is_available"],
                name="radio_stati_is_avai_973d05_idx",
            ),
        ),
        migrations.CreateModel(
            name="StationHealthCheck",
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
                (
                    "checked_at",
                    models.DateTimeField(auto_now_add=True, db_index=True),
                ),
                ("is_reachable", models.BooleanField()),
                ("response_time_ms", models.IntegerField(blank=True, null=True)),
                ("status_code", models.IntegerField(blank=True, null=True)),
                ("error_message", models.TextField(blank=True)),
                (
                    "station",
                    models.ForeignKey(
                        on_delete=models.deletion.CASCADE,
                        related_name="health_checks",
                        to="radio.station",
                    ),
                ),
            ],
            options={
                "verbose_name": "Station health check",
                "verbose_name_plural": "Station health checks",
                "db_table": "radio_station_health_check",
                "ordering": ["-checked_at"],
            },
        ),
        migrations.AddIndex(
            model_name="stationhealthcheck",
            index=models.Index(
                fields=["station", "-checked_at"],
                name="radio_stati_station_bb44e7_idx",
            ),
        ),
    ]
