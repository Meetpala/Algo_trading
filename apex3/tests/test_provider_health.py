"""Per-provider session diagnostics, 15s status caching, and /health isolation (Zerodha vs Angel One)."""
from unittest.mock import MagicMock, patch

from apex3.broker.angel_data_provider import AngelDataProvider
from apex3.broker.hybrid_adapter import HybridBrokerAdapter
from apex3.broker.kite_adapter import KiteAdapter
from apex3.config import AppConfig
from apex3.dashboard.app import create_app
from apex3.strategy.engine import StrategyEngine


def _exec(valid=True, status="CONNECTED", error=None):
    b = MagicMock()
    b.is_session_valid.return_value = valid
    b.get_session_diagnostics.return_value = {
        "session_valid": valid, "status": status, "user_id": "ESG968", "error": error,
    }
    return b


def _data(valid=True, status="CONNECTED", error=None):
    d = MagicMock()
    d.is_session_valid.return_value = valid
    d.get_session_diagnostics.return_value = {
        "session_valid": valid, "status": status, "client_code": "KEKJ1463", "error": error,
    }
    return d


def test_providers_status_isolates_failing_provider():
    hybrid = HybridBrokerAdapter(
        data_provider=_data(False, "NETWORK_ERROR", "ConnectionError: reset"),
        execution_broker=_exec(True),
    )
    st = hybrid.get_providers_status()
    assert st["zerodha"]["session_valid"] is True
    assert st["angel_one"]["session_valid"] is False
    assert st["angel_one"]["status"] == "NETWORK_ERROR"


def test_providers_status_is_cached_for_15s_and_refreshes_after():
    exec_b, data_p = _exec(True), _data(True)
    hybrid = HybridBrokerAdapter(data_provider=data_p, execution_broker=exec_b)
    with patch("apex3.broker.hybrid_adapter.time.monotonic") as mono:
        mono.return_value = 1000.0
        first = hybrid.get_providers_status()
        mono.return_value = 1010.0  # 10s later -> served from cache
        second = hybrid.get_providers_status()
        assert exec_b.get_session_diagnostics.call_count == 1
        assert data_p.get_session_diagnostics.call_count == 1
        assert first["cached"] is False and second["cached"] is True
        mono.return_value = 1016.0  # >15s -> fresh probe
        third = hybrid.get_providers_status()
        assert exec_b.get_session_diagnostics.call_count == 2
        assert third["cached"] is False


def test_probe_exception_never_propagates():
    bad = MagicMock()
    bad.get_session_diagnostics.side_effect = RuntimeError("boom")
    hybrid = HybridBrokerAdapter(data_provider=_data(True), execution_broker=bad)
    st = hybrid.get_providers_status()
    assert st["zerodha"]["session_valid"] is False
    assert "boom" in st["zerodha"]["error"]


def test_health_blames_angel_not_zerodha_and_returns_503():
    cfg = AppConfig()
    hybrid = HybridBrokerAdapter(
        data_provider=_data(False, "NETWORK_ERROR", "ReadTimeout"),
        execution_broker=_exec(True),
    )
    engine = StrategyEngine(cfg, hybrid, MagicMock())
    client = create_app(engine, cfg, MagicMock()).test_client()
    res = client.get("/health")
    body = res.get_json()
    assert res.status_code == 503
    assert body["providers"]["zerodha"]["session_valid"] is True
    assert body["providers"]["angel_one"]["status"] == "NETWORK_ERROR"
    assert any(r.startswith("Angel One") for r in body["degraded_reasons"])
    assert not any(r.startswith("Zerodha") for r in body["degraded_reasons"])
    assert "/login/kite" not in body["readiness"]["action_required"]
    assert "Angel One" in body["readiness"]["action_required"]
    assert body["readiness"]["can_trade_live"] is False


def test_health_zerodha_token_expiry_requests_login():
    cfg = AppConfig()
    hybrid = HybridBrokerAdapter(
        data_provider=_data(True),
        execution_broker=_exec(False, "TOKEN_EXPIRED", "TokenException: Incorrect api_key or access_token"),
    )
    engine = StrategyEngine(cfg, hybrid, MagicMock())
    body = create_app(engine, cfg, MagicMock()).test_client().get("/health").get_json()
    assert body["providers"]["zerodha"]["status"] == "TOKEN_EXPIRED"
    assert "/login/kite" in body["readiness"]["action_required"]
    assert not any(r.startswith("Angel One") for r in body["degraded_reasons"])


def test_health_healthy_when_both_connected():
    cfg = AppConfig()
    hybrid = HybridBrokerAdapter(data_provider=_data(True), execution_broker=_exec(True))
    engine = StrategyEngine(cfg, hybrid, MagicMock())
    res = create_app(engine, cfg, MagicMock()).test_client().get("/health")
    body = res.get_json()
    assert res.status_code == 200
    assert body["status"] == "healthy" and body["degraded_reasons"] == []
    assert body["readiness"]["can_trade_live"] is True


def test_angel_diagnostics_is_read_only_never_logs_in():
    prov = AngelDataProvider(api_key="k", client_code="C1", pin="1", totp_secret="S")
    prov.authenticate = MagicMock()
    d = prov.get_session_diagnostics()  # no session established
    assert d["session_valid"] is False and d["status"] == "NOT_AUTHENTICATED"
    prov.authenticate.assert_not_called()

    prov._smart_api = MagicMock()
    prov._auth_token = "jwt"
    prov._refresh_token = "r"
    prov._smart_api.getProfile.side_effect = ConnectionError("reset by peer")
    d = prov.get_session_diagnostics()
    assert d["status"] == "NETWORK_ERROR" and "reset" in d["error"]
    prov.authenticate.assert_not_called()


def test_kite_diagnostics_distinguish_network_from_expired_token():
    adapter = KiteAdapter(api_key="k", access_token="tok", live_enabled=False)
    adapter._kite = MagicMock()
    adapter.is_session_valid = MagicMock(return_value=False)

    adapter._kite.profile.side_effect = Exception("HTTPSConnectionPool: Max retries exceeded, timed out")
    assert adapter.get_session_diagnostics()["status"] == "NETWORK_ERROR"

    adapter._kite.profile.side_effect = Exception("TokenException: Incorrect `api_key` or `access_token`.")
    assert adapter.get_session_diagnostics()["status"] == "TOKEN_EXPIRED"

    adapter._kite.profile.side_effect = None
    adapter._kite.profile.return_value = {"user_id": "ESG968"}
    d = adapter.get_session_diagnostics()
    assert d["session_valid"] is True and d["user_id"] == "ESG968"
