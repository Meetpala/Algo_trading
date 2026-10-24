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
from dotenv import load_dotenv

load_dotenv()


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
    max_symbols: int = 2
    max_leverage: float = 4.0
    min_accepted_leverage: float = 4.0
    safety_reserve_pct: float = 5.0


@dataclass(frozen=True)
class RiskConfig:
    risk_per_trade_pct: float = 0.80
    max_combined_open_risk_pct: float = 1.60
    daily_loss_limit_pct: float = 1.60
    slippage_allowance_pct: float = 0.0


@dataclass(frozen=True)
class RegimeConfig:
    benchmark_symbol: str = "NIFTY 50"
    benchmark_exchange: str = "NSE"
    ema_period: int = 20
    ema_fast_period: int = 20
    ema_slow_period: int = 50
    ema_slope_lookback: int = 3
    min_adx: float = 22.0
    adx_period: int = 14
    require_vwap_alignment: bool = True
    require_rising_adx: bool = True


@dataclass(frozen=True)
class SignalConfig:
    min_relative_volume: float = 1.5
    volume_avg_period: int = 20
    max_spread_bps: float = 8.0
    min_circuit_distance_pct: float = 2.0
    candle_timeframe_entry: str = "5min"
    candle_timeframe_trail: str = "1min"
    relative_strength_lookback: int = 20
    min_stop_distance_pct: float = 0.0055
    max_stop_distance_pct: float = 0.0090
    min_breakout_cushion_atr: float = 0.15
    min_clv_ratio: float = 0.70
    min_atr_pct: float = 0.0012


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
    swing_lookback_bars: int = 5
    sl_limit_buffer_pct: float = 0.25
    target_r: float = 2.0
    use_fixed_target: bool = True
    trail_enabled: bool = False
    fee_lock_enabled: bool = True
    fee_lock_trigger_r: float = 1.60
    fee_lock_profit_r: float = 0.60
    late_day_lock_trigger_r: float = 1.15
    late_day_lock_profit_r: float = 0.35
    late_day_protection_time: str = "14:15"
    fee_lock_offset_pct: float = 0.0015
    trail: TrailConfig = field(default_factory=TrailConfig)
    max_modifications_per_order: int = 25
    modification_headroom: int = 3


@dataclass(frozen=True)
class ScheduleConfig:
    market_open: str = "09:15"
    opening_range_end: str = "09:30"
    entry_start: str = "09:35"
    entry_cutoff: str = "12:30"
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
    exchange_txn_pct: float = 0.00318
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
    angel_api_key: str = ""
    angel_client_code: str = ""
    angel_pin: str = ""
    angel_totp_secret: str = ""
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
    raw_dict: dict = field(default_factory=dict)


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
        angel_api_key=os.environ.get("ANGEL_API_KEY", ""),
        angel_client_code=os.environ.get("ANGEL_CLIENT_CODE", ""),
        angel_pin=os.environ.get("ANGEL_PIN", ""),
        angel_totp_secret=os.environ.get("ANGEL_TOTP_SECRET", ""),
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
        market_open=str(raw.get("or_start_time", raw.get("market_open", "09:15"))),
        opening_range_end=str(raw.get("or_end_time", raw.get("opening_range_end", "09:30"))),
        entry_start=str(raw.get("entry_start_time", raw.get("entry_start", "09:35"))),
        entry_cutoff=str(raw.get("entry_cutoff_time", raw.get("entry_cutoff", "12:30"))),
        flatten_start=str(raw.get("square_off_time", raw.get("flatten_start", "14:55"))),
        flat_target=str(raw.get("flat_verification_time", raw.get("flat_target", "14:58"))),
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

    # Support both new 3-tier schema and baseline schema
    acc_raw = raw.get("account", {})
    risk_raw = raw.get("risk_management", raw.get("risk", {}))
    regime_raw = raw.get("regime_filter", raw.get("regime", {}))
    signal_raw = raw.get("signal_gates", raw.get("signal", {}))
    stops_raw = raw.get("stops_and_targets", raw.get("stops", {}))
    schedule_raw = raw.get("session_schedule", raw.get("schedule", {}))
    radar_raw = raw.get("radar", {})
    watchlist_raw = raw.get("watchlist", {})
    limits_raw = raw.get("limits", {})
    entry_raw = raw.get("entry", {})
    rate_limits_raw = raw.get("rate_limits", {})
    costs_raw = raw.get("costs", {})
    journal_raw = raw.get("journal", {})
    dashboard_raw = raw.get("dashboard", {})

    # Build merged watchlist from radar tiers if present
    merged_symbols = list(radar_raw.get("core_watchlist", []))
    if radar_raw.get("enable_extended_radar", True):
        merged_symbols.extend(radar_raw.get("extended_radar", []))
    merged_symbols.extend(radar_raw.get("custom_user_radar", []))
    if not merged_symbols:
        merged_symbols = watchlist_raw.get("symbols", [])

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

    safety_pct = float(acc_raw.get("safety_reserve_buffer_pct", limits_raw.get("safety_reserve_pct", 5.0)))
    if safety_pct <= 1.0:
        safety_pct *= 100.0

    risk_per_trade = float(risk_raw.get("risk_per_trade_pct", 0.80))
    if risk_per_trade <= 0.1:
        risk_per_trade *= 100.0

    daily_loss = float(risk_raw.get("daily_loss_limit_pct", 1.60))
    if daily_loss <= 0.1:
        daily_loss *= 100.0

    circuit_dist = float(signal_raw.get("min_circuit_distance_pct", 2.0))
    if circuit_dist <= 0.1:
        circuit_dist *= 100.0

    return AppConfig(
        mode=str(raw.get("mode", "paper")),
        paper_starting_capital=float(acc_raw.get("starting_capital", raw.get("paper_starting_capital", 15_000.0))),
        watchlist=WatchlistConfig(
            symbols=merged_symbols,
            scanner_enabled=bool(watchlist_raw.get("scanner_enabled", bool(radar_raw))),
            scanner_universe=merged_symbols,
        ),
        limits=LimitsConfig(
            max_symbols=int(risk_raw.get("max_concurrent_positions", limits_raw.get("max_symbols", 2))),
            max_leverage=float(acc_raw.get("max_intraday_leverage", limits_raw.get("max_leverage", 4.0))),
            min_accepted_leverage=float(limits_raw.get("min_accepted_leverage", 4.0)),
            safety_reserve_pct=safety_pct,
        ),
        risk=RiskConfig(
            risk_per_trade_pct=risk_per_trade,
            max_combined_open_risk_pct=float(risk_raw.get("max_combined_open_risk_pct", 1.60)),
            daily_loss_limit_pct=daily_loss,
            slippage_allowance_pct=float(risk_raw.get("slippage_allowance_pct", 0.0)),
        ),
        regime=RegimeConfig(
            benchmark_symbol=str(regime_raw.get("benchmark_symbol", "NIFTY 50")),
            benchmark_exchange=str(regime_raw.get("benchmark_exchange", "NSE")),
            ema_period=int(regime_raw.get("ema_period", 20)),
            ema_fast_period=int(regime_raw.get("ema_fast_period", 20)),
            ema_slow_period=int(regime_raw.get("ema_slow_period", 50)),
            ema_slope_lookback=int(regime_raw.get("ema_slope_lookback", 3)),
            min_adx=float(regime_raw.get("min_adx", 22.0)),
            adx_period=int(regime_raw.get("adx_period", 14)),
            require_vwap_alignment=bool(regime_raw.get("require_vwap_alignment", True)),
            require_rising_adx=bool(regime_raw.get("require_rising_adx", True)),
        ),
        signal=SignalConfig(
            min_relative_volume=float(signal_raw.get("min_volume_surge_ratio", signal_raw.get("min_relative_volume", 2.0))),
            volume_avg_period=int(signal_raw.get("volume_avg_bars", signal_raw.get("volume_avg_period", 20))),
            max_spread_bps=float(signal_raw.get("max_spread_bps", 4.0)),
            min_circuit_distance_pct=circuit_dist,
            candle_timeframe_entry=str(signal_raw.get("candle_timeframe_entry", "5min")),
            candle_timeframe_trail=str(signal_raw.get("candle_timeframe_trail", "1min")),
            relative_strength_lookback=int(signal_raw.get("rs_lookback_bars", signal_raw.get("relative_strength_lookback", 20))),
            min_stop_distance_pct=float(signal_raw.get("min_stop_distance_pct", 0.0055)),
            max_stop_distance_pct=float(signal_raw.get("max_stop_distance_pct", 0.0090)),
            min_breakout_cushion_atr=float(signal_raw.get("min_breakout_cushion_atr", 0.15)),
            min_clv_ratio=float(signal_raw.get("min_clv_ratio", 0.70)),
            min_atr_pct=float(signal_raw.get("min_atr_pct", 0.0012)),
        ),
        entry=EntryConfig(
            order_type=str(entry_raw.get("order_type", "LIMIT")),
            max_slippage_pct=float(entry_raw.get("max_slippage_pct", 0.15)),
            unfilled_timeout_sec=int(entry_raw.get("unfilled_timeout_sec", 30)),
            max_retries=int(entry_raw.get("max_retries", 1)),
        ),
        stops=StopsConfig(
            initial_atr_multiplier=float(stops_raw.get("atr_stop_multiplier", stops_raw.get("initial_atr_multiplier", 1.5))),
            initial_atr_period=int(stops_raw.get("atr_period", stops_raw.get("initial_atr_period", 14))),
            swing_lookback_bars=int(stops_raw.get("swing_lookback_bars", 5)),
            sl_limit_buffer_pct=float(stops_raw.get("sl_limit_buffer_pct", 0.25)),
            target_r=float(stops_raw.get("target_r_multiple", stops_raw.get("target_r", 2.0))),
            use_fixed_target=bool(stops_raw.get("use_fixed_target", True)),
            trail_enabled=bool(stops_raw.get("trail_enabled", False)),
            fee_lock_enabled=bool(stops_raw.get("fee_lock_enabled", True)),
            fee_lock_trigger_r=float(stops_raw.get("fee_lock_trigger_r", 1.60)),
            fee_lock_profit_r=float(stops_raw.get("fee_lock_profit_r", 0.60)),
            late_day_lock_trigger_r=float(stops_raw.get("late_day_lock_trigger_r", 1.15)),
            late_day_lock_profit_r=float(stops_raw.get("late_day_lock_profit_r", 0.35)),
            late_day_protection_time=str(schedule_raw.get("late_day_protection_time", stops_raw.get("late_day_protection_time", "14:15"))),
            fee_lock_offset_pct=float(stops_raw.get("fee_lock_offset_pct", 0.0015)),
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
            exchange_txn_pct=float(costs_raw.get("exchange_txn_pct", 0.00318)),
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
        raw_dict=raw,
    )
