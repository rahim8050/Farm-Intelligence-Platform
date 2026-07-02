import json

import pytest
from django.core.cache import cache

from activities.dead_letter import (
    DEAD_LETTER_INDEX_KEY,
    _dl_key,
    list_dead_letters,
    replay_dead_letter,
)

pytestmark = pytest.mark.django_db


def test_list_dead_letters_corrupt_json() -> None:
    cache.set(DEAD_LETTER_INDEX_KEY, [999])
    cache.set(_dl_key(999), "not valid json")

    entries = list_dead_letters()
    assert len(entries) == 0


def test_replay_dead_letter_does_not_exist() -> None:
    # Insert a fake dead letter for an activity that does not exist in DB
    cache.set(DEAD_LETTER_INDEX_KEY, [888])
    cache.set(_dl_key(888), json.dumps({"activity_id": 888}))

    # Replay should catch DoesNotExist, clean up cache, and return True
    result = replay_dead_letter(888)
    assert result is True

    # Verify cache is cleaned up
    assert cache.get(_dl_key(888)) is None
    index = cache.get(DEAD_LETTER_INDEX_KEY, [])
    assert 888 not in index
