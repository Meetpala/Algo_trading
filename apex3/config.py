"""APEX-3 Configuration Loader.

Loads strategy parameters from config.yaml and secrets from
environment variables. Secrets are never logged or stored in
configuration files.

Usage:
    config = load_config()
    config = load_config("path/to/custom_config.yaml")
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, time
from pathlib import Path
from typing import Optional

import yaml


# ---------------------------------------------------------------------------
# Configuration dataclasses
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class WatchlistConfig:
    symbols: list[str] = field(default_factory=list)
    scanner_enabled: bool = False
    scanner_universe: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class LimitsConfig:
    max_symbols: int = 3
    max_leverage: float = 4.0
    min_accepted_leverage: float = 4.0
    safety_reserve_pct: float = 5.0


@dataclass(frozen=True)
class RiskConfig:
    risk_per_trade_pct: float = 0.25
    max_combined_open_risk_pct: float = 0.75
    daily_loss_limit_pct: float = 1.0
    slippage_allowance_pct: float = 0.10


@dataclass(frozen=True)
class RegimeConfig:
    benchmark_symbol: str = "NIFTY 50"
    benchmark_exchange: str = "NSE"
    ema_period: int = 20
    ema_slope_lookback: int = 3
    min_adx: float = 22.0
    adx_period: int = 14


@dataclass(frozen=True)
class SignalConfig:
    min_relative_volume: float = 1.5
    volume_avg_period: int = 20
    max_spread_bps: float = 8.0
    min_circuit_distance_pct: float = 2.0
    candle_timeframe_entry: str = "5min"
    candle_timeframe_trail: str = "1min"
    relative_strength_lookback: int = 20


@dataclass(frozen=True)
class EntryConfig:
    order_type: str = "LIMIT"
    max_slippage_pct: float = 0.15
    unfilled_timeout_sec: int = 30
    max_retries: int = 1


@dataclass(frozen=True)
class TrailConfig:
    activation_r: float = 1.0
    profit_lock_rupees: float = 0.50
    atr_multiplier_normal: float = 2.5
    atr_multiplier_tight: float = 1.5
    min_step_ticks: int = 2
    swing_pad_ticks: int = 2
    exhaustion_adx_threshold: float = 18.0
    late_session_tighten_time: str = "13:30"


@dataclass(frozen=True)
class StopsConfig:
    initial_atr_multiplier: float = 1.5
    initial_atr_period: int = 14
    sl_limit_buffer_pct: float = 0.25
    target_r: float = 2.0
    use_fixed_target: bool = True
    trail_enabled: bool = False
    trail: TrailConfig = field(default_factory=TrailConfig)
    max_modifications_per_order: int = 25
    modification_headroom: int = 3


@dataclass(frozen=True)
class ScheduleConfig:
    market_open: str = "09:15"
    opening_range_end: str = "09:30"
    entry_start: str = "09:30"
    entry_cutoff: str = "14:15"
    flatten_start: str = "14:55"
    flat_target: str = "14:58"
    market_close: str = "15:30"
    hard_deadline: str = "15:00"
    stale_threshold_sec: int = 2
    holidays: list[str] = field(default_factory=list)

    def get_time(self, field_name: str) -> time:
        """Parse an HH:MM time string from the config."""
        val = getattr(self, field_name)
        parts = val.split(":")
        return time(int(parts[0]), int(parts[1]))

    def is_holiday(self, check_date: date) -> bool:
        """Check if a date is in the configured holiday list."""
        date_str = check_date.isoformat()
        return date_str in self.holidays


@dataclass(frozen=True)
class RateLimitsConfig:
    orders_per_second: int = 10
    orders_per_minute: int = 200
    quotes_per_second: int = 1
    historical_per_second: int = 3
    general_per_second: int = 10


@dataclass(frozen=True)
class CostsConfig:
    brokerage_per_order: float = 20.0
    stt_pct: float = 0.025
    exchange_txn_pct: float = 0.00307
    gst_pct: float = 18.0
    sebi_turnover_pct: float = 0.0001
    stamp_duty_pct: float = 0.003


@dataclass(frozen=True)
class JournalConfig:
    db_path: str = "apex3_journal.db"
    log_decisions: bool = True
    log_orders: bool = True
    log_fills: bool = True
    log_stop_changes: bool = True
    log_warnings: bool = True
    log_daily_summary: bool = True


@dataclass(frozen=True)
class DashboardConfig:
    host: str = "127.0.0.1"
    port: int = 5000
    refresh_interval_sec: int = 2


@dataclass(frozen=True)
class SecretsConfig:
    """Secrets loaded from environment variables only.

    WARNING: Never log, print, serialize, or store these values
    in files, commits, screenshots, or frontend code.
    """
    kite_api_key: str = ""
    kite_api_secret: str = ""
    kite_access_token: str = ""
    live_trading_enabled: bool = False
    static_ip_configured: bool = False
    sebi_compliance_acknowledged: bool = False


@dataclass
class AppConfig:
    """Complete application configuration."""
    mode: str = "paper"
    paper_starting_capital: float = 15_000.0
    watchlist: WatchlistConfig = field(default_factory=WatchlistConfig)
    limits: LimitsConfig = field(default_factory=LimitsConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    regime: RegimeConfig = field(default_factory=RegimeConfig)
    signal: SignalConfig = field(default_factory=SignalConfig)
    entry: EntryConfig = field(default_factory=EntryConfig)
    stops: StopsConfig = field(default_factory=StopsConfig)
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)
    rate_limits: RateLimitsConfig = field(default_factory=RateLimitsConfig)
    costs: CostsConfig = field(default_factory=CostsConfig)
    journal: JournalConfig = field(default_factory=JournalConfig)
    dashboard: DashboardConfig = field(default_factory=DashboardConfig)
    secrets: SecretsConfig = field(default_factory=SecretsConfig)


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

def _load_secrets() -> SecretsConfig:
    """Load secrets from environment variables.

    Never reads from files, config, or defaults to real values.
    """
    return SecretsConfig(
        kite_api_key=os.environ.get("KITE_API_KEY", ""),
        kite_api_secret=os.environ.get("KITE_API_SECRET", ""),
        kite_access_token=os.environ.get("KITE_ACCESS_TOKEN", ""),
        live_trading_enabled=os.environ.get(
            "LIVE_TRADING_ENABLED", "false"
        ).lower() == "true",
        static_ip_configured=os.environ.get(
            "STATIC_IP_CONFIGURED", "false"
        ).lower() == "true",
        sebi_compliance_acknowledged=os.environ.get(
            "SEBI_COMPLIANCE_ACKNOWLEDGED", "false"
        ).lower() == "true",
    )


def _parse_schedule(raw: dict) -> ScheduleConfig:
    """Parse schedule section, collecting holidays from dated keys."""
    holidays: list[str] = []
    for key, value in raw.items():
        if key.startswith("holidays_") and isinstance(value, list):
            holidays.extend(str(v) for v in value)
    return ScheduleConfig(
        market_open=str(raw.get("market_open", "09:15")),
        opening_range_end=str(raw.get("opening_range_end", "09:30")),
        entry_start=str(raw.get("entry_start", "09:30")),
        entry_cutoff=str(raw.get("entry_cutoff", "14:15")),
        flatten_start=str(raw.get("flatten_start", "14:55")),
        flat_target=str(raw.get("flat_target", "14:58")),
        market_close=str(raw.get("market_close", "15:30")),
        hard_deadline=str(raw.get("hard_deadline", "15:00")),
        stale_threshold_sec=int(raw.get("stale_threshold_sec", 2)),
        holidays=holidays,
    )


def load_config(config_path: Optional[str] = None) -> AppConfig:
    """Load application configuration from YAML file and environment.

    Args:
        config_path: Path to config.yaml. If None, uses the default
                     config.yaml in the apex3 package directory.

    Returns:
        Fully populated AppConfig instance.
    """
    if config_path is None:
        config_path = str(Path(__file__).parent / "config.yaml")

    with open(config_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    # Parse each section with defaults
    watchlist_raw = raw.get("watchlist", {})
    limits_raw = raw.get("limits", {})
    risk_raw = raw.get("risk", {})
    regime_raw = raw.get("regime", {})
    signal_raw = raw.get("signal", {})
    entry_raw = raw.get("entry", {})
    stops_raw = raw.get("stops", {})
    schedule_raw = raw.get("schedule", {})
    rate_limits_raw = raw.get("rate_limits", {})
    costs_raw = raw.get("costs", {})
    journal_raw = raw.get("journal", {})
    dashboard_raw = raw.get("dashboard", {})

    # Build trail config
    trail_raw = stops_raw.get("trail", {})
    trail_config = TrailConfig(
        activation_r=float(trail_raw.get("activation_r", 1.0)),
        profit_lock_rupees=float(trail_raw.get("profit_lock_rupees", 0.50)),
        atr_multiplier_normal=float(trail_raw.get("atr_multiplier_normal", 2.5)),
        atr_multiplier_tight=float(trail_raw.get("atr_multiplier_tight", 1.5)),
        min_step_ticks=int(trail_raw.get("min_step_ticks", 2)),
        swing_pad_ticks=int(trail_raw.get("swing_pad_ticks", 2)),
        exhaustion_adx_threshold=float(trail_raw.get("exhaustion_adx_threshold", 18.0)),
        late_session_tighten_time=str(trail_raw.get("late_session_tighten_time", "13:30")),
    )

    return AppConfig(
        mode=str(raw.get("mode", "paper")),
        paper_starting_capital=float(raw.get("paper_starting_capital", 15_000.0)),
        watchlist=WatchlistConfig(
            symbols=watchlist_raw.get("symbols", []),
            scanner_enabled=bool(watchlist_raw.get("scanner_enabled", False)),
            scanner_universe=watchlist_raw.get("scanner_universe", []),
        ),
        limits=LimitsConfig(
            max_symbols=int(limits_raw.get("max_symbols", 3)),
            max_leverage=float(limits_raw.get("max_leverage", 4.0)),
            min_accepted_leverage=float(limits_raw.get("min_accepted_leverage", 4.0)),
            safety_reserve_pct=float(limits_raw.get("safety_reserve_pct", 5.0)),
        ),
        risk=RiskConfig(
            risk_per_trade_pct=float(risk_raw.get("risk_per_trade_pct", 0.25)),
            max_combined_open_risk_pct=float(risk_raw.get("max_combined_open_risk_pct", 0.75)),
            daily_loss_limit_pct=float(risk_raw.get("daily_loss_limit_pct", 1.0)),
            slippage_allowance_pct=float(risk_raw.get("slippage_allowance_pct", 0.10)),
        ),
        regime=RegimeConfig(
            benchmark_symbol=str(regime_raw.get("benchmark_symbol", "NIFTY 50")),
            benchmark_exchange=str(regime_raw.get("benchmark_exchange", "NSE")),
            ema_period=int(regime_raw.get("ema_period", 20)),
            ema_slope_lookback=int(regime_raw.get("ema_slope_lookback", 3)),
            min_adx=float(regime_raw.get("min_adx", 22.0)),
            adx_period=int(regime_raw.get("adx_period", 14)),
        ),
        signal=SignalConfig(
            min_relative_volume=float(signal_raw.get("min_relative_volume", 1.5)),
            volume_avg_period=int(signal_raw.get("volume_avg_period", 20)),
            max_spread_bps=float(signal_raw.get("max_spread_bps", 8.0)),
            min_circuit_distance_pct=float(signal_raw.get("min_circuit_distance_pct", 2.0)),
            candle_timeframe_entry=str(signal_raw.get("candle_timeframe_entry", "5min")),
            candle_timeframe_trail=str(signal_raw.get("candle_timeframe_trail", "1min")),
            relative_strength_lookback=int(signal_raw.get("relative_strength_lookback", 20)),
        ),
        entry=EntryConfig(
            order_type=str(entry_raw.get("order_type", "LIMIT")),
            max_slippage_pct=float(entry_raw.get("max_slippage_pct", 0.15)),
            unfilled_timeout_sec=int(entry_raw.get("unfilled_timeout_sec", 30)),
            max_retries=int(entry_raw.get("max_retries", 1)),
        ),
        stops=StopsConfig(
            initial_atr_multiplier=float(stops_raw.get("initial_atr_multiplier", 1.5)),
            initial_atr_period=int(stops_raw.get("initial_atr_period", 14)),
            sl_limit_buffer_pct=float(stops_raw.get("sl_limit_buffer_pct", 0.25)),
            target_r=float(stops_raw.get("target_r", 2.0)),
            use_fixed_target=bool(stops_raw.get("use_fixed_target", True)),
            trail_enabled=bool(stops_raw.get("trail_enabled", False)),
            trail=trail_config,
            max_modifications_per_order=int(stops_raw.get("max_modifications_per_order", 25)),
            modification_headroom=int(stops_raw.get("modification_headroom", 3)),
        ),
        schedule=_parse_schedule(schedule_raw),
        rate_limits=RateLimitsConfig(
            orders_per_second=int(rate_limits_raw.get("orders_per_second", 10)),
            orders_per_minute=int(rate_limits_raw.get("orders_per_minute", 200)),
            quotes_per_second=int(rate_limits_raw.get("quotes_per_second", 1)),
            historical_per_second=int(rate_limits_raw.get("historical_per_second", 3)),
            general_per_second=int(rate_limits_raw.get("general_per_second", 10)),
        ),
        costs=CostsConfig(
            brokerage_per_order=float(costs_raw.get("brokerage_per_order", 20.0)),
            stt_pct=float(costs_raw.get("stt_pct", 0.025)),
            exchange_txn_pct=float(costs_raw.get("exchange_txn_pct", 0.00307)),
            gst_pct=float(costs_raw.get("gst_pct", 18.0)),
            sebi_turnover_pct=float(costs_raw.get("sebi_turnover_pct", 0.0001)),
            stamp_duty_pct=float(costs_raw.get("stamp_duty_pct", 0.003)),
        ),
        journal=JournalConfig(
            db_path=str(journal_raw.get("db_path", "apex3_journal.db")),
            log_decisions=bool(journal_raw.get("log_decisions", True)),
            log_orders=bool(journal_raw.get("log_orders", True)),
            log_fills=bool(journal_raw.get("log_fills", True)),
            log_stop_changes=bool(journal_raw.get("log_stop_changes", True)),
            log_warnings=bool(journal_raw.get("log_warnings", True)),
            log_daily_summary=bool(journal_raw.get("log_daily_summary", True)),
        ),
        dashboard=DashboardConfig(
            host=str(dashboard_raw.get("host", "127.0.0.1")),
            port=int(dashboard_raw.get("port", 5000)),
            refresh_interval_sec=int(dashboard_raw.get("refresh_interval_sec", 2)),
        ),
        secrets=_load_secrets(),
    )
