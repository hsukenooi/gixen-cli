"""HTTP endpoint tests — GixenClient is mocked, DB uses tmp_path."""
import pytest
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _make_mock_gixen():
    m = MagicMock()
    m.list_snipes.return_value = []
    m.add_snipe.return_value = None
    m.modify_snipe.return_value = None
    m.remove_snipe.return_value = True
    m.purge_completed.return_value = None
    return m


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("GIXEN_USERNAME", "testuser")
    monkeypatch.setenv("GIXEN_PASSWORD", "testpass")
    monkeypatch.setenv("GIXEN_SYNC_ENABLED", "false")
    mock = _make_mock_gixen()
    with patch("server.main.GixenClient", return_value=mock):
        from server.main import app
        with TestClient(app) as client:
            client.mock_gixen = mock
            yield client


def test_health(api):
    r = api.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_upsert_comic(api):
    r = api.post("/api/comics", json={
        "title": "Amazing Spider-Man",
        "issue": "300",
        "year": 1988,
        "grade": 9.2,
        "fmv_low": 800.0,
        "fmv_high": 1000.0,
        "fmv_comps": 12,
        "fmv_confidence": "high",
        "fmv_notes": "Key issue",
    })
    assert r.status_code == 200
    data = r.json()
    assert data["id"] > 0
    assert data["title"] == "Amazing Spider-Man"


def test_upsert_comic_twice_updates(api):
    payload = {"title": "X-Men", "issue": "1", "year": 1963,
               "grade": 8.0, "fmv_low": 500.0, "fmv_high": 700.0,
               "fmv_comps": 5, "fmv_confidence": "medium", "fmv_notes": ""}
    r1 = api.post("/api/comics", json=payload)
    payload["fmv_low"] = 550.0
    r2 = api.post("/api/comics", json=payload)
    assert r1.json()["id"] == r2.json()["id"]
    assert r2.json()["fmv_low"] == 550.0


def test_upsert_comic_missing_required_field(api):
    r = api.post("/api/comics", json={"title": "X-Men", "issue": "1"})  # missing year
    assert r.status_code == 422


def test_add_bid_no_comic(api):
    r = api.post("/api/bids", json={
        "item_id": "123456789",
        "max_bid": 50.0,
    })
    assert r.status_code == 200
    data = r.json()
    assert data["item_id"] == "123456789"
    assert data["status"] == "PENDING"
    api.mock_gixen.add_snipe.assert_called_once()


def test_add_bid_with_comic_links_fmv(api):
    r = api.post("/api/bids", json={
        "item_id": "987654321",
        "max_bid": 800.0,
        "comic": "Amazing Spider-Man",
        "issue": "300",
        "year": 1988,
        "grade": 9.2,
        "fmv_low": 800.0,
        "fmv_high": 1000.0,
        "fmv_comps": 12,
        "fmv_confidence": "high",
        "fmv_notes": "",
    })
    assert r.status_code == 200
    data = r.json()
    assert data["comic_id"] is not None


def test_add_bid_invalid_item_id(api):
    r = api.post("/api/bids", json={"item_id": "abc", "max_bid": 50.0})
    assert r.status_code == 422


def test_add_bid_negative_max_bid(api):
    r = api.post("/api/bids", json={"item_id": "123456789", "max_bid": -10.0})
    assert r.status_code == 422


def test_add_bid_gixen_error_returns_503(api):
    from gixen_client import GixenError
    api.mock_gixen.add_snipe.side_effect = GixenError("Gixen down")
    r = api.post("/api/bids", json={"item_id": "111222333", "max_bid": 50.0})
    assert r.status_code == 503
