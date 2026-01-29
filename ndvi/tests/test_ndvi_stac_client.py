from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from ndvi.engines.base import BBox
from ndvi.stac_client import StacClient, select_best_item


def test_stac_client_search_filters_and_selects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = StacClient(
        base_url="https://example.com/stac/",
        collection="collection",
        timeout_seconds=1,
    )
    response_data: dict[str, object] = {
        "features": [
            {
                "id": "low-cloud-close",
                "properties": {
                    "datetime": "2025-01-02T10:00:00Z",
                    "eo:cloud_cover": 10,
                },
                "assets": {"B04": {"href": "https://example.com/red.tif"}},
            },
            {
                "id": "high-cloud",
                "properties": {
                    "datetime": "2025-01-02T10:00:00Z",
                    "eo:cloud_cover": 80,
                },
                "assets": {"B04": {"href": "https://example.com/red2.tif"}},
            },
            {
                "id": "no-cloud",
                "properties": {"datetime": "2025-01-03T10:00:00Z"},
                "assets": {"B04": {"href": "https://example.com/red3.tif"}},
            },
        ],
        "links": [],
    }

    class FakeResponse:
        def json(self) -> dict[str, object]:
            return response_data

    monkeypatch.setattr(
        client, "_request", lambda *_args, **_kwargs: FakeResponse()
    )

    items = client.search(
        bbox=BBox(
            south=Decimal("0.0"),
            west=Decimal("0.0"),
            north=Decimal("0.1"),
            east=Decimal("0.1"),
        ),
        start=date(2025, 1, 1),
        end=date(2025, 1, 5),
        max_cloud=30,
    )
    assert len(items) == 2

    best = select_best_item(
        items,
        target_date=date(2025, 1, 2),
        window_days=3,
    )
    assert best is not None
    assert best.id == "low-cloud-close"
