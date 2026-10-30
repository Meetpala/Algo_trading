"""Angel One SmartAPI Market Data Provider for APEX-3.

STRICTLY READ-ONLY:
This provider is exclusively designed for streaming real-time quotes, 
5-minute candles, and market depth from Angel One's 100% free SmartAPI.
It does NOT contain or expose any order execution, modification, or cancellation
capabilities. All trading orders are routed exclusively through the execution broker (Zerodha).
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from typing import Any, Optional
from zoneinfo import ZoneInfo

from apex3.broker.rate_limiter import RateLimiter
from apex3.models import Candle, InstrumentInfo, Quote

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")

# Static token mapping for top liquid NIFTY candidates (cached for sub-millisecond lookup)
DEFAULT_ANGEL_TOKENS: dict[str, str] = {
    # Audited Elite 10 Institutional Watchlist
    "HINDALCO": "1363",
    "JSWSTEEL": "11723",
    "RELIANCE": "2885",
    "MARUTI": "10999",
    "ITC": "1660",
    "TCS": "11536",
    "SBIN": "3045",
    "TRENT": "1964",
    "KOTAKBANK": "1922",
    "SUNPHARMA": "3351",
    # Tier 2 & Extended Radar Symbols
    "BHARTIARTL": "10604",
    "JIOFIN": "18147",
    "TATASTEEL": "3499",
    "AXISBANK": "5900",
    "TATAPOWER": "3426",
    # Benchmark & High Liquidity Proxies
    "NIFTY 50": "99926000",
    "NIFTY": "99926000",
    "NSEI": "99926000",
    "^NSEI": "99926000",
    "INFY": "1594",
    "HDFCBANK": "1333",
    "ICICIBANK": "4963",
    "LT": "11483",
    "BAJFINANCE": "317",
    "HINDUNILVR": "1394",
    "ASIANPAINT": "236",
}


class AngelDataProvider:
    """Read-only market data provider powered by Angel One SmartAPI."""

    def __init__(
        self,
        api_key: str = "",
        client_code: str = "",
        pin: str = "",
        totp_secret: str = "",
        rate_limiter: Optional[RateLimiter] = None,
    ) -> None:
        self.api_key = api_key
        self.client_code = client_code
        self.pin = pin
        self.totp_secret = totp_secret
        self._rate_limiter = rate_limiter

        self._smart_api: Optional[Any] = None
        self._auth_token: Optional[str] = None
        self._refresh_token: Optional[str] = None
        self._feed_token: Optional[str] = None
        self._token_cache: dict[str, str] = dict(DEFAULT_ANGEL_TOKENS)
        self._circuit_cache: dict[str, tuple[float, float]] = {}
        self._candle_cache: dict[tuple[str, str], tuple[float, list[Candle]]] = {}
        self._candle_cache_ttl: float = 120.0
        self._error_backoff: dict[tuple[str, str], float] = {}

        if api_key and client_code and pin and totp_secret:
            self.authenticate()

    def authenticate(self) -> bool:
        """Authenticate with Angel One SmartAPI using TOTP."""
        try:
            from SmartApi import SmartConnect
            import pyotp

            logger.info("Authenticating with Angel One SmartAPI (Client: %s)...", self.client_code)
            self._smart_api = SmartConnect(api_key=self.api_key)

            totp_val = pyotp.TOTP(self.totp_secret).now()
            session_data = self._smart_api.generateSession(self.client_code, self.pin, totp_val)

            if not session_data or not session_data.get("status"):
                msg = session_data.get("message", "Unknown error") if session_data else "Empty response"
                logger.error("Angel One session generation failed: %s", msg)
                return False

            data = session_data.get("data", {})
            self._auth_token = data.get("jwtToken")
            self._refresh_token = data.get("refreshToken")
            self._feed_token = self._smart_api.getfeedToken()
            logger.info("Angel One SmartAPI authentication SUCCESSFUL! Free market data feed active.")
            return True
        except ImportError:
            logger.warning("smartapi-python or pyotp not installed. Angel data feed disabled.")
            return False
        except Exception as e:
            logger.error("Failed to connect to Angel One SmartAPI: %s", e)
            return False

    def is_session_valid(self) -> bool:
        """Verify that Angel One SmartAPI session is active and authenticated."""
        if not self._smart_api or not self._auth_token:
            return False
        try:
            if hasattr(self._smart_api, "getProfile"):
                prof = self._smart_api.getProfile(self._refresh_token)
                if prof and prof.get("status") is False:
                    return False
            return True
        except Exception as e:
            logger.warning("Angel One session validity check failed: %s", e)
            return False

    def resolve_token(self, symbol: str, exchange: str = "NSE") -> str:
        """Resolve exchange symbol token for Angel One. Fails closed on unmapped symbols."""
        clean = symbol.replace("-EQ", "").upper()
        if clean in self._token_cache:
            return self._token_cache[clean]
        logger.error("Unknown Angel One token mapping for symbol %s in %s (no default fallback)", clean, exchange)
        return ""

    def _parse_market_item(self, item: dict, symbol: str, exchange: str) -> Quote:
        """Parse raw SmartAPI getMarketData item into Quote with strict freshness checks."""
        clean = symbol.replace("-EQ", "").upper()
        token = str(item.get("symbolToken", ""))
        is_index = clean in ("NIFTY 50", "NIFTY", "NSEI", "^NSEI", "NIFTY BANK", "BANKNIFTY") or token == "99926000"

        ltp = float(item.get("ltp", 0.0) or 0.0)
        depth = item.get("depth", {})
        buy_levels = depth.get("buy", []) if isinstance(depth, dict) else []
        sell_levels = depth.get("sell", []) if isinstance(depth, dict) else []

        best_bid = float(buy_levels[0].get("price", 0.0) or 0.0) if buy_levels else 0.0
        best_ask = float(sell_levels[0].get("price", 0.0) or 0.0) if sell_levels else 0.0
        vol = int(item.get("tradeVolume", item.get("volume", 0)) or 0)

        # Cache exchange circuit limits
        up_c = float(item.get("upperCircuit", 0.0) or 0.0)
        low_c = float(item.get("lowerCircuit", 0.0) or 0.0)
        if up_c > 0 and low_c > 0:
            self._circuit_cache[clean] = (low_c, up_c)

        # Parse exchange feed timestamp (FAIL CLOSED IF MISSING — do NOT invent from local time)
        feed_str = item.get("exchFeedTime") or item.get("exchTradeTime")
        feed_dt = None
        if feed_str:
            for fmt in ("%d-%b-%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d/%m/%Y %H:%M:%S"):
                try:
                    feed_dt = datetime.strptime(feed_str, fmt).replace(tzinfo=IST)
                    break
                except Exception:
                    pass

        now_ist = datetime.now(IST)
        if feed_dt is not None:
            age_sec = (now_ist - feed_dt).total_seconds()
            is_time_fresh = (-15.0 <= age_sec <= 15.0)
            quote_ts = feed_dt
        else:
            is_time_fresh = False
            quote_ts = now_ist

        if is_index:
            # Index quotes do not have order-book depth on the exchange.
            # Validity requires real exchange timestamp and positive LTP.
            is_fresh = bool(feed_dt is not None and is_time_fresh and ltp > 0.0)
        else:
            # Stock quotes require real exchange timestamp, positive LTP, and valid top-of-book depth
            is_valid_depth = (best_bid > 0.0 and best_ask > 0.0 and best_ask >= best_bid)
            is_fresh = bool(feed_dt is not None and is_time_fresh and ltp > 0.0 and is_valid_depth)

        return Quote(
            symbol=clean,
            exchange=exchange,
            last_price=ltp,
            bid=best_bid,
            ask=best_ask,
            volume=vol,
            timestamp=quote_ts,
            is_fresh=is_fresh,
        )

    def get_quotes_batch(self, symbols: list[str], exchange: str = "NSE") -> dict[str, Quote]:
        """Fetch live quote snapshots for multiple symbols in a single batch request."""
        quotes: dict[str, Quote] = {}
        now_ist = datetime.now(IST)

        clean_symbols = [s.replace("-EQ", "").upper() for s in symbols]
        tokens_map: dict[str, str] = {}
        for sym in clean_symbols:
            tok = self.resolve_token(sym, exchange)
            if tok:
                tokens_map[tok] = sym
            else:
                quotes[sym] = Quote(
                    symbol=sym,
                    exchange=exchange,
                    last_price=0.0,
                    bid=0.0,
                    ask=0.0,
                    volume=0,
                    timestamp=now_ist,
                    is_fresh=False,
                )

        if not tokens_map or not self._smart_api:
            for sym in clean_symbols:
                if sym not in quotes:
                    quotes[sym] = Quote(
                        symbol=sym,
                        exchange=exchange,
                        last_price=0.0,
                        bid=0.0,
                        ask=0.0,
                        volume=0,
                        timestamp=now_ist,
                        is_fresh=False,
                    )
            return quotes

        if self._rate_limiter:
            acquired = self._rate_limiter.acquire("quotes", timeout=2.0)
            if not acquired:
                logger.warning("Rate limit timeout acquiring quotes token for batch (%d symbols)", len(tokens_map))
                for sym in clean_symbols:
                    if sym not in quotes:
                        quotes[sym] = Quote(
                            symbol=sym,
                            exchange=exchange,
                            last_price=0.0,
                            bid=0.0,
                            ask=0.0,
                            volume=0,
                            timestamp=now_ist,
                            is_fresh=False,
                        )
                return quotes

        token_list = list(tokens_map.keys())
        chunk_size = 50
        for i in range(0, len(token_list), chunk_size):
            chunk = token_list[i : i + chunk_size]
            try:
                if hasattr(self._smart_api, "getMarketData"):
                    m_data = self._smart_api.getMarketData(
                        mode="FULL",
                        exchangeTokens={exchange: chunk},
                    )
                    if m_data and m_data.get("status") and "data" in m_data:
                        fetched = m_data["data"].get("fetched", [])
                        for item in fetched:
                            tok = str(item.get("symbolToken", ""))
                            sym = tokens_map.get(tok)
                            if not sym:
                                ts = str(item.get("tradingSymbol", "")).replace("-EQ", "").upper()
                                if ts in clean_symbols:
                                    sym = ts
                            if not sym and len(chunk) == 1 and len(clean_symbols) == 1:
                                sym = clean_symbols[0]
                            if sym:
                                quotes[sym] = self._parse_market_item(item, sym, exchange)
            except Exception as e:
                logger.error("Error in getMarketData batch chunk: %s", e)

        # Fallback for any symbols missing in batch response
        for sym in clean_symbols:
            if sym not in quotes:
                quotes[sym] = Quote(
                    symbol=sym,
                    exchange=exchange,
                    last_price=0.0,
                    bid=0.0,
                    ask=0.0,
                    volume=0,
                    timestamp=now_ist,
                    is_fresh=False,
                )

        return quotes

    def get_quote(self, symbol: str, exchange: str = "NSE") -> Quote:
        """Fetch live quote snapshot from Angel One with true market depth and exchange timestamp."""
        res = self.get_quotes_batch([symbol], exchange=exchange)
        clean = symbol.replace("-EQ", "").upper()
        return res.get(
            clean,
            Quote(
                symbol=clean,
                exchange=exchange,
                last_price=0.0,
                bid=0.0,
                ask=0.0,
                volume=0,
                timestamp=datetime.now(IST),
                is_fresh=False,
            ),
        )

    def get_historical(
        self,
        symbol: str,
        interval: str,
        from_dt: Optional[datetime] = None,
        to_dt: Optional[datetime] = None,
        exchange: str = "NSE",
    ) -> list[Candle]:
        """Fetch historical candles from Angel One."""
        clean = symbol.replace("-EQ", "").upper()
        token = self.resolve_token(clean, exchange)

        if from_dt is None:
            from_dt = datetime.now(IST) - timedelta(days=5)
        if to_dt is None:
            to_dt = datetime.now(IST)

        # Normalize interval ("5min" -> "FIVE_MINUTE", "1min" -> "ONE_MINUTE")
        angel_interval = "FIVE_MINUTE"
        if interval in ("1min", "1m", "minute"):
            angel_interval = "ONE_MINUTE"
        elif interval in ("15min", "15m"):
            angel_interval = "FIFTEEN_MINUTE"
        elif interval in ("1d", "day"):
            angel_interval = "ONE_DAY"

        # Check short-term candle cache to prevent burst exhaustion
        cache_key = (clean, interval)
        now_mono = time.monotonic()
        if cache_key in self._candle_cache:
            cached_t, cached_candles = self._candle_cache[cache_key]
            if (now_mono - cached_t) < self._candle_cache_ttl and cached_candles:
                return cached_candles

        # Check error backoff cooldown (e.g. rate limit 429)
        if cache_key in self._error_backoff:
            if now_mono < self._error_backoff[cache_key]:
                if cache_key in self._candle_cache and self._candle_cache[cache_key][1]:
                    return self._candle_cache[cache_key][1]
                return []
            else:
                del self._error_backoff[cache_key]

        if self._smart_api:
            if self._rate_limiter:
                acquired = self._rate_limiter.acquire("historical", timeout=4.0)
                if not acquired:
                    logger.warning("Rate limit timeout acquiring historical token for %s", clean)
                    if cache_key in self._candle_cache and self._candle_cache[cache_key][1]:
                        logger.info("Serving stale cached candles for %s due to rate limit timeout", clean)
                        return self._candle_cache[cache_key][1]
                    return []

            try:
                hist_params = {
                    "exchange": exchange,
                    "symboltoken": token,
                    "interval": angel_interval,
                    "fromdate": from_dt.strftime("%Y-%m-%d %H:%M"),
                    "todate": to_dt.strftime("%Y-%m-%d %H:%M"),
                }
                res = self._smart_api.getCandleData(hist_params)
                # Space calls to respect Angel One rate limit
                time.sleep(0.35)
                if res and res.get("status") and "data" in res:
                    candles = []
                    for row in res["data"]:
                        t_str = row[0]
                        dt = datetime.fromisoformat(t_str.replace("Z", "+00:00")).astimezone(IST)
                        now_ist = datetime.now(IST)
                        int_upper = str(interval).upper()
                        if "1MIN" in int_upper or "1M" in int_upper or "ONE_MIN" in int_upper or int_upper == "MINUTE":
                            tf_min = 1
                        elif "3MIN" in int_upper or "3M" in int_upper or "THREE_MIN" in int_upper:
                            tf_min = 3
                        elif "15MIN" in int_upper or "15M" in int_upper or "FIFTEEN" in int_upper:
                            tf_min = 15
                        elif "30MIN" in int_upper or "30M" in int_upper or "THIRTY" in int_upper:
                            tf_min = 30
                        elif "60MIN" in int_upper or "60M" in int_upper or "1H" in int_upper or "HOUR" in int_upper:
                            tf_min = 60
                        elif "1D" in int_upper or "DAY" in int_upper:
                            tf_min = 1440
                        else:
                            tf_min = 5
                        is_done = now_ist >= (dt + timedelta(minutes=tf_min))
                        candles.append(
                            Candle(
                                timestamp=dt,
                                open=float(row[1]),
                                high=float(row[2]),
                                low=float(row[3]),
                                close=float(row[4]),
                                volume=int(row[5]),
                                is_complete=is_done,
                            )
                        )
                    if candles:
                        self._candle_cache[cache_key] = (now_mono, candles)
                    return candles
            except Exception as e:
                logger.error("Angel One get_historical error for %s: %s", clean, e)
                self._error_backoff[cache_key] = now_mono + 10.0  # 10s cooldown
                if cache_key in self._candle_cache and self._candle_cache[cache_key][1]:
                    return self._candle_cache[cache_key][1]

        return []

    def get_instrument_info(self, symbol: str, exchange: str = "NSE") -> InstrumentInfo:
        """Fetch instrument info with verified circuit limits from exchange."""
        clean = symbol.replace("-EQ", "").upper()
        token = self.resolve_token(clean, exchange)
        low_c, up_c = self._circuit_cache.get(clean, (0.0, 0.0))

        return InstrumentInfo(
            symbol=clean,
            exchange=exchange,
            tick_size=0.05,
            lot_size=1,
            instrument_token=token,
            upper_circuit=up_c,
            lower_circuit=low_c,
            mis_available=True,
        )
