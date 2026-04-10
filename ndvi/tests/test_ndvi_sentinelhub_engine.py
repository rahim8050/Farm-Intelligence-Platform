from __future__ import annotations

# ruff: noqa: S101
import secrets
from datetime import date
from decimal import Decimal

import httpx
import pytest

from ndvi.engines.base import BBox, NdviPoint
from ndvi.engines.sentinelhub import (
    SentinelHubEngine,
    SentinelHubUpstreamError,
)

CLIENT_SECRET = secrets.token_urlsafe(12)


def test_sentinelhub_requires_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SENTINELHUB_CLIENT_ID", raising=False)
    monkeypatch.delenv("SENTINELHUB_CLIENT_SECRET", raising=False)
    with pytest.raises(ValueError, match="client credentials"):
        SentinelHubEngine()


def test_sentinelhub_get_timeseries_uses_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = SentinelHubEngine(client_id="cid", client_secret=CLIENT_SECRET)

    class FakeResponse:
        def json(self) -> dict[str, object]:
            return {"data": []}

    monkeypatch.setattr(engine, "_get_access_token", lambda: "token")
    monkeypatch.setattr(
        engine, "_request_with_retry", lambda *_, **__: FakeResponse()
    )
    monkeypatch.setattr(
        engine,
        "_parse_statistics_response",
        lambda *_: [NdviPoint(date=date(2025, 1, 1), mean=0.2)],
    )
    points = engine.get_timeseries(
        bbox=BBox(
            south=Decimal("0.0"),
            west=Decimal("0.0"),
            north=Decimal("0.1"),
            east=Decimal("0.1"),
        ),
        start=date(2025, 1, 1),
        end=date(2025, 1, 8),
        step_days=7,
        max_cloud=20,
    )
    assert len(points) == 1


def test_sentinelhub_get_latest_handles_empty() -> None:
    engine = SentinelHubEngine(client_id="cid", client_secret=CLIENT_SECRET)
    engine.get_timeseries = lambda **_: []  # type: ignore[assignment]
    assert (
        engine.get_latest(
            bbox=BBox(
                south=Decimal("0.0"),
                west=Decimal("0.0"),
                north=Decimal("0.1"),
                east=Decimal("0.1"),
            ),
            lookback_days=7,
            max_cloud=20,
        )
        is None
    )


def test_sentinelhub_get_latest_returns_last_point() -> None:
    engine = SentinelHubEngine(client_id="cid", client_secret=CLIENT_SECRET)
    engine.get_timeseries = lambda **_: [  # type: ignore[assignment]
        NdviPoint(date=date(2025, 1, 1), mean=0.1),
        NdviPoint(date=date(2025, 1, 8), mean=0.2),
    ]
    latest = engine.get_latest(
        bbox=BBox(
            south=Decimal("0.0"),
            west=Decimal("0.0"),
            north=Decimal("0.1"),
            east=Decimal("0.1"),
        ),
        lookback_days=7,
        max_cloud=20,
    )
    assert latest is not None
    assert latest.date == date(2025, 1, 8)


def test_sentinelhub_request_with_retry_http_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = SentinelHubEngine(client_id="cid", client_secret=CLIENT_SECRET)
    calls = {"count": 0}

    class FakeResponse:
        def __init__(self, status_code: int = 200) -> None:
            self.status_code = status_code

        def raise_for_status(self) -> None:
            if self.status_code >= 400:
                request = httpx.Request("GET", "https://example.com")
                response = httpx.Response(self.status_code)
                raise httpx.HTTPStatusError(
                    "boom", request=request, response=response
                )

    def fake_request(*_: object, **__: object) -> FakeResponse:
        calls["count"] += 1
        if calls["count"] == 1:
            return FakeResponse(status_code=502)
        return FakeResponse(status_code=200)

    monkeypatch.setattr(engine._http, "request", fake_request)
    monkeypatch.setattr("ndvi.engines.sentinelhub.time.sleep", lambda *_: None)
    resp = engine._request_with_retry("GET", "https://example.com")
    assert isinstance(resp, FakeResponse)
    assert calls["count"] == 2


def test_sentinelhub_request_with_retry_network_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = SentinelHubEngine(client_id="cid", client_secret=CLIENT_SECRET)

    def fake_request(*_: object, **__: object) -> None:
        raise httpx.RequestError("network", request=httpx.Request("GET", "x"))

    monkeypatch.setattr(engine._http, "request", fake_request)
    monkeypatch.setattr("ndvi.engines.sentinelhub.time.sleep", lambda *_: None)
    with pytest.raises(SentinelHubUpstreamError, match="network"):
        engine._request_with_retry(
            "GET", "https://example.com", max_attempts=2
        )


def test_sentinelhub_request_zero_attempts() -> None:
    engine = SentinelHubEngine(client_id="cid", client_secret=CLIENT_SECRET)
    with pytest.raises(RuntimeError, match="Unknown upstream error"):
        engine._request_with_retry(
            "GET", "https://example.com", max_attempts=0
        )


def test_sentinelhub_get_access_token_requires_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = SentinelHubEngine(client_id="cid", client_secret=CLIENT_SECRET)

    class FakeResponse:
        def json(self) -> dict[str, object]:
            return {"expires_in": 3600}

    monkeypatch.setattr(
        engine, "_request_with_retry", lambda *_, **__: FakeResponse()
    )
    with pytest.raises(ValueError, match="missing access_token"):
        engine._get_access_token()


def test_sentinelhub_build_statistics_payload() -> None:
    engine = SentinelHubEngine(client_id="cid", client_secret=CLIENT_SECRET)
    payload = engine._build_statistics_payload(
        bbox=BBox(
            south=Decimal("1.0"),
            west=Decimal("2.0"),
            north=Decimal("3.0"),
            east=Decimal("4.0"),
        ),
        start=date(2025, 1, 1),
        end=date(2025, 1, 8),
        step_days=7,
        max_cloud=20,
    )
    assert payload["input"]["bounds"]["bbox"] == [2.0, 1.0, 4.0, 3.0]
    assert payload["aggregation"]["aggregationInterval"]["of"] == "P7D"
    assert "evalscript" in payload["aggregation"]


def test_sentinelhub_parse_statistics_response() -> None:
    engine = SentinelHubEngine(client_id="cid", client_secret=CLIENT_SECRET)
    data = {
        "data": [
            {"interval": {}},
            {"interval": {"from": "bad-date"}},
            {
                "interval": {"from": "2025-01-01T00:00:00Z"},
                "outputs": {
                    "default": {
                        "statistics": {
                            "ndvi": {
                                "stats": {
                                    "mean": "0.5",
                                    "min": "0.1",
                                    "max": "0.9",
                                    "sampleCount": 4,
                                }
                            }
                        },
                        "cloudCoverage": 0.2,
                    }
                },
            },
            {
                "interval": {"from": "2025-01-08"},
                "outputs": {
                    "default": {"statistics": {"ndvi": {"stats": {}}}}
                },
            },
            {
                "interval": {"from": "2025-01-15"},
                "outputs": {
                    "default": {
                        "bands": {
                            "NDVI": {"stats": {"mean": 0.3, "count": 2}}
                        },
                        "cloudFraction": 0.1,
                    }
                },
            },
        ]
    }
    points = engine._parse_statistics_response(data)
    assert len(points) == 2
    assert points[0].mean == 0.5
    assert points[0].sample_count == 4
    assert points[0].cloud_fraction == pytest.approx(0.2)
    assert points[1].mean == pytest.approx(0.3)
    assert points[1].sample_count == 2
    assert points[1].cloud_fraction == pytest.approx(0.1)
