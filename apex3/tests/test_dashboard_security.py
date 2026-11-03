"""Operator token must never be disclosed to unauthenticated network clients."""
from unittest.mock import MagicMock

from apex3.config import AppConfig
from apex3.dashboard.app import create_app
from apex3.strategy.engine import StrategyEngine

EXTERNAL = {"REMOTE_ADDR": "203.0.113.50"}  # documentation-range address, i.e. not loopback


def _client(monkeypatch, token="s3cret-operator-token"):
    monkeypatch.setenv("DASHBOARD_API_TOKEN", token)
    cfg = AppConfig()
    object.__setattr__(cfg.dashboard, "host", "0.0.0.0")  # same as the VM's `--host 0.0.0.0`
    broker = MagicMock()
    broker.is_session_valid.return_value = True
    engine = StrategyEngine(cfg, broker, MagicMock())
    return create_app(engine, cfg, MagicMock()).test_client()


def test_public_dashboard_page_does_not_leak_operator_token(monkeypatch):
    client = _client(monkeypatch)
    res = client.get("/", environ_overrides=EXTERNAL)
    assert res.status_code == 200
    assert "s3cret-operator-token" not in res.get_data(as_text=True)


def test_mutating_endpoints_reject_unauthenticated_external_clients(monkeypatch):
    client = _client(monkeypatch)
    hdrs = {"Origin": "http://localhost"}
    for path in ("/api/emergency_flatten", "/api/pause", "/api/test_order", "/api/set_token", "/api/watchlist"):
        r = client.post(path, json={}, headers=hdrs, environ_overrides=EXTERNAL)
        assert r.status_code == 401, path
    assert client.get("/api/status", environ_overrides=EXTERNAL).status_code == 401


def test_valid_operator_token_still_authorizes(monkeypatch):
    client = _client(monkeypatch)
    ok = client.post(
        "/api/pause", json={"pause": True},
        headers={"X-Operator-Token": "s3cret-operator-token", "Origin": "http://localhost"},
        environ_overrides=EXTERNAL,
    )
    assert ok.status_code == 200 and ok.get_json()["is_paused"] is True
    bad = client.post(
        "/api/pause", json={"pause": False},
        headers={"X-Operator-Token": "wrong", "Origin": "http://localhost"},
        environ_overrides=EXTERNAL,
    )
    assert bad.status_code == 401


def test_health_and_oauth_callback_stay_reachable_without_token(monkeypatch):
    client = _client(monkeypatch)
    assert client.get("/ping", environ_overrides=EXTERNAL).status_code == 200
    assert client.get("/health", environ_overrides=EXTERNAL).status_code in (200, 503)
    assert client.get("/login/kite", environ_overrides=EXTERNAL).status_code in (302, 400)
