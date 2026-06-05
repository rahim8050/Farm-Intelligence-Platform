"""Add per-feed backoff state to ``Podcast``.

Per ``prompts/p4-staff-engineer-review.md`` #3 the periodic feed
refresh is split into one Celery task per podcast so they run in
parallel, and a misbehaving feed must not stall the others. A
single backoff column (``next_retry_at``) is enough: a 429 / 5xx
response from the upstream RSS server bumps the next allowed
refresh by 1m -> 5m -> 1h -> 24h; a 200 response clears it.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("podcasts", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="podcast",
            name="next_retry_at",
            field=models.DateTimeField(
                blank=True,
                help_text=(
                    "Earliest time the next refresh is allowed. Set to "
                    "the future when a feed errors out (exponential "
                    "backoff: 1m, 5m, 1h, 24h); cleared on success."
                ),
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="podcast",
            name="consecutive_failures",
            field=models.PositiveSmallIntegerField(
                default=0,
                help_text=(
                    "Number of consecutive failed refreshes. Drives "
                    "the exponential backoff schedule (1m, 5m, 1h, "
                    "24h); reset to 0 on the first success."
                ),
            ),
        ),
        migrations.AddIndex(
            model_name="podcast",
            index=models.Index(
                condition=models.Q(is_active=True),
                fields=["next_retry_at"],
                name="podcasts_po_active_retry_idx",
            ),
        ),
    ]
