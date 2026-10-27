"""Angel One SmartAPI Market Data Provider for APEX-3.

STRICTLY READ-ONLY:
This provider is exclusively designed for streaming real-time quotes, 
5-minute candles, and market depth from Angel One's 100% free SmartAPI.
It does NOT contain or expose any order execution, modification, or cancellation
capabilities. All trading orders are routed exclusively through the execution broker (Zerodha).
"""
from __future__ import annotations

import logging
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
        api_key: str,
        client_code: str,
        pin: str,
        totp_secret: str,
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

    def get_quote(self, symbol: str, exchange: str = "NSE") -> Quote:
        """Fetch live quote snapshot from Angel One with true market depth and exchange timestamp."""
        clean = symbol.replace("-EQ", "").upper()
        token = self.resolve_token(clean, exchange)
        if not token:
            logger.error("Refusing quote fetch for unmapped symbol %s; failing closed.", clean)
            return Quote(
                symbol=clean,
                exchange=exchange,
                last_price=0.0,
                bid=0.0,
                ask=0.0,
                volume=0,
                timestamp=datetime.now(IST),
                is_fresh=False,
            )

        if self._smart_api:
            # 1. Enforce rate limiting
            if self._rate_limiter:
                acquired = self._rate_limiter.acquire("quotes", timeout=2.0)
                if not acquired:
                    logger.warning("Rate limit timeout acquiring quotes token for %s", clean)
                    return Quote(
                        symbol=clean,
                        exchange=exchange,
                        last_price=0.0,
                        bid=0.0,
                        ask=0.0,
                        volume=0,
                        timestamp=datetime.now(IST),
                        is_fresh=False,
                    )

            # 2. Query FULL market data with orderbook depth and exchange timestamps
            try:
                if hasattr(self._smart_api, "getMarketData"):
                    m_data = self._smart_api.getMarketData(
                        mode="FULL",
                        exchangeTokens={exchange: [token]},
                    )
                    if m_data and m_data.get("status") and "data" in m_data:
                        fetched = m_data["data"].get("fetched", [])
                        if fetched:
                            item = fetched[0]
                            ltp = float(item.get("ltp", 0.0))
                            depth = item.get("depth", {})
                            buy_levels = depth.get("buy", []) if isinstance(depth, dict) else []
                            sell_levels = depth.get("sell", []) if isinstance(depth, dict) else []

                            best_bid = float(buy_levels[0].get("price", 0.0)) if buy_levels else 0.0
                            best_ask = float(sell_levels[0].get("price", 0.0)) if sell_levels else 0.0
                            vol = int(item.get("tradeVolume", item.get("volume", 0)) or 0)

                            # Cache exchange circuit limits
                            up_c = float(item.get("upperCircuit", 0.0) or 0.0)
                            low_c = float(item.get("lowerCircuit", 0.0) or 0.0)
                            if up_c > 0 and low_c > 0:
                                self._circuit_cache[clean] = (low_c, up_c)

                            # Parse exchange feed timestamp
                            feed_str = item.get("exchFeedTime") or item.get("exchTradeTime")
                            feed_dt = None
                            if feed_str:
                                for fmt in ("%d-%b-%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d/%m/%Y %H:%M:%S"):
                                    try:
                                        feed_dt = datetime.strptime(feed_str, fmt).replace(tzinfo=IST)
                                        break
                                    except Exception:
                                        pass
                            if not feed_dt:
                                feed_dt = datetime.now(IST)

                            # Validate market depth and spread validity
                            is_valid_depth = (best_bid > 0 and best_ask > 0 and best_ask >= best_bid)
                            is_fresh = bool(ltp > 0 and is_valid_depth)

                            return Quote(
                                symbol=clean,
                                exchange=exchange,
                                last_price=ltp,
                                bid=best_bid,
                                ask=best_ask,
                                volume=vol,
                                timestamp=feed_dt,
                                is_fresh=is_fresh,
                            )
            except Exception as e_full:
                logger.debug("getMarketData FULL not available or failed for %s: %s; trying ltpData fallback", clean, e_full)

            # 3. Secondary fallback: ltpData (LTP only, no depth -> bid=0, ask=0, marked is_fresh=False)
            try:
                trading_symbol = f"{clean}-EQ" if exchange == "NSE" and clean != "NIFTY" else clean
                res = self._smart_api.ltpData(
                    exchange=exchange,
                    tradingsymbol=trading_symbol,
                    symboltoken=token,
                )
                if res and res.get("status") and "data" in res:
                    data = res["data"]
                    ltp = float(data.get("ltp", 0.0))
                    return Quote(
                        symbol=clean,
                        exchange=exchange,
                        last_price=ltp,
                        bid=0.0,
                        ask=0.0,
                        volume=int(data.get("volume", 0)),
                        timestamp=datetime.now(IST),
                        is_fresh=False,  # Fails closed on entry gates due to lack of verified depth
                    )
            except Exception as e:
                logger.error("Angel One get_quote error for %s: %s", clean, e)

        # Fail closed: never fabricate prices
        return Quote(
            symbol=clean,
            exchange=exchange,
            last_price=0.0,
            bid=0.0,
            ask=0.0,
            volume=0,
            timestamp=datetime.now(IST),
            is_fresh=False,
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

        if self._smart_api:
            if self._rate_limiter:
                acquired = self._rate_limiter.acquire("historical", timeout=3.0)
                if not acquired:
                    logger.warning("Rate limit timeout acquiring historical token for %s", clean)
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
                if res and res.get("status") and "data" in res:
                    candles = []
                    for row in res["data"]:
                        t_str = row[0]
                        dt = datetime.fromisoformat(t_str.replace("Z", "+00:00")).astimezone(IST)
                        now_ist = datetime.now(IST)
                        tf_min = 5 if "FIVE" in str(interval).upper() else 5
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
                    return candles
            except Exception as e:
                logger.error("Angel One get_historical error for %s: %s", clean, e)

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
