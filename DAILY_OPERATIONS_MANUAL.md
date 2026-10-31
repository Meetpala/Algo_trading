# APEX-3: Daily Operations Manual & Pre-Flight Routine

> **Trading Market**: NSE Cash Equities (MIS Intraday Only)  
> **Broker Architecture**: Hybrid Split-Feed (Angel One SmartAPI for Free Live Market Data + Zerodha Kite Connect for Execution)  
> **Server Infrastructure**: Google Cloud Platform (GCP Compute Engine) + Docker Container

---

## 1. Executive Overview: Who Does What Every Morning?

| Component | Morning Requirement | How It Authenticates | Human Action Required? |
| :--- | :--- | :--- | :---: |
| **Angel One SmartAPI** (Market Data Feed) | Generates session JWT & feed token for live 5m candles | **100% Fully Automated** via `pyotp` TOTP secret in `.env` | **NO (Zero Manual Action)** |
| **Zerodha Kite Connect** (Execution Broker) | Generates daily access token (expires daily at 06:00 AM IST) | **1-Click Browser Login** via Cloudflare Tunnel or local IP | **YES (1-Click at 08:45 AM)** |
| **Strategy Engine** (Autonomous Execution) | Evaluates 12-Step pipeline every 5m bar | **Autonomous Background Process** (`docker-compose`) | **NO (Runs 24/7 on Cloud)** |

---

## 2. Angel One SmartAPI: Market Data Authentication Explained

The system uses **Angel One SmartAPI** as a dedicated, read-only market data provider to stream live tick data, real-time quotes, and 5-minute candles for **`NIFTY 50`** and the **Elite 10 Stocks** (`HINDALCO`, `JSWSTEEL`, `RELIANCE`, `MARUTI`, `ITC`, `TCS`, `SBIN`, `TRENT`, `KOTAKBANK`, `SUNPHARMA`).

### Required Credentials in `.env`
All four credentials are permanently stored in your cloud `.env` file (never commit real secrets to git):
```ini
ANGEL_API_KEY=<YOUR_ANGEL_API_KEY>
ANGEL_CLIENT_CODE=<YOUR_ANGEL_CLIENT_CODE>
ANGEL_PIN=<YOUR_ANGEL_PIN>
ANGEL_TOTP_SECRET=<YOUR_ANGEL_TOTP_SECRET>
```

### How the Engine Authenticates Headlessly (Zero Human Intervention)
Unlike Zerodha, **Angel One does NOT require you to open a browser or copy-paste anything**:
1. When the APEX-3 engine starts up, [`AngelDataProvider.authenticate()`](file:///c:/Users/itsme/OneDrive/Desktop/Algo%20Trading/apex3/broker/angel_data_provider.py) generates a fresh 6-digit TOTP code automatically in memory using `pyotp.TOTP(self.totp_secret).now()`.
2. It sends `(client_code, pin, totp)` directly to Angel One's authentication endpoint.
3. Angel One responds with a valid session token (`jwtToken`) and `feedToken`.
4. The system is immediately ready to stream quotes and build candles.

### How to Test Angel One Connectivity Anytime
From your terminal, you can run this one-liner to verify that Angel One is feeding live data:
```bash
python -c "
import os; from dotenv import load_dotenv; load_dotenv()
from apex3.broker.angel_data_provider import AngelDataProvider
angel = AngelDataProvider(os.getenv('ANGEL_API_KEY'), os.getenv('ANGEL_CLIENT_CODE'), os.getenv('ANGEL_PIN'), os.getenv('ANGEL_TOTP_SECRET'))
q = angel.get_quote('TRENT')
print('Angel One Live Status: SUCCESS! TRENT LTP:', q.last_price, '| Fresh:', q.is_fresh)
"
```

---

## 3. Daily Timeline & Operator Checklist

Print or bookmark this daily schedule:

```
08:30 - 09:10 IST ──► [Step 1: 1-Click Zerodha Morning Login]
                            │
09:15 - 09:30 IST ──► [Step 2: Opening Range (OR) Build Window — NO TRADES]
                            │
09:35 - 12:30 IST ──► [Step 3: Autonomous Entry Window — 10 Elite Stocks]
                            │
12:30 IST         ──► [Step 4: Hard Entry Cutoff — Manage Existing Only]
                            │
14:15 IST         ──► [Step 5: Late-Day Fee Protection Step (+1.15R / +1.65R)]
                            │
14:55 IST         ──► [Step 6: Orderly MIS Square-Off — Flattens all to 0]
                            │
15:00 - 15:30 IST ──► [Step 7: Daily Reconciliation & P&L Summary]
```

---

## 4. Exact Step-by-Step Daily Routine

### Step 1: Morning 1-Click Login (08:30 – 09:10 AM IST)
Zerodha Kite Connect access tokens expire daily between 07:00 AM and 07:30 AM IST during Zerodha's backend maintenance reset. Therefore, you must authorize your daily session once every morning after 07:30 AM:

1. Open your phone or laptop browser and tap your permanent Cloud Dashboard login link:
   ```
   http://34.14.147.10:5000/login/kite
   ```
2. Log in with your **Zerodha User ID**, **Password**, and approve the **Mobile 2FA App TOTP**.
3. Zerodha redirects back directly to `http://34.14.147.10:5000/auth/callback`.
4. The cloud engine automatically exchanges the token, saves it into `.env`, and updates the running strategy engine in memory.
5. Your dashboard at `http://34.14.147.10:5000` displays: **`Mode: LIVE | Session: VALID`**.
   *That's it! Your daily human input is finished.*

> **Permanent Setup Note (Set & Forget)**:  
> In your **Zerodha Developer Console** (https://developers.kite.trade), ensure your App's **Redirect URL** is set to:  
> `http://34.14.147.10:5000/auth/callback`  
> Because `34.14.147.10` is your cloud VM's permanent IP, this URL never changes and you will never need to edit the Zerodha console again.

*(Fallback CLI method if accessing directly via SSH)*:
```bash
python scripts/login_zerodha.py
```

---

### Step 2: Opening Range Construction (09:15 – 09:30 AM IST)
- At **09:15 IST**, Indian cash equity markets open.
- **Rule**: The strategy engine is in **`BUILDING_OPENING_RANGE`** state.
- **Strict Quantitative Rule**: Zero entries are permitted during the first 15 minutes. The system tracks high and low boundaries for the benchmark `NIFTY 50` and the 10 Elite stocks.

---

### Step 3: Morning Stability & Execution Window (09:35 – 12:30 IST)
- At **09:35 IST** (after the first post-OR 5-minute candle closes), the execution engine evaluates all 8 quantitative gates on every 5-minute candle close:
  1. **Dual-EMA Trend Filter**: NIFTY 50 `Close > EMA20 > EMA50` for Long (or `<` for Short) with rising ADX $\ge 22.0$.
  2. **Session VWAP Alignment**: Stock price above VWAP for Long, below for Short.
  3. **Relative Strength (RS)**: Stock outperforming NIFTY 50 over 20 bars.
  4. **Volume Surge**: Current 5m volume $\ge 2.0\times$ 20-bar SMA.
  5. **Breakout Cushion**: Breakout close must clear the OR high/low by $\ge 0.15\times \text{ATR}_{14}$.
  6. **Candle CLV**: Close Location Value $\ge 0.70$ (closes in top/bottom 30% of bar).
  7. **Stop-Width Sweet Spot**: Risk distance bounded between $0.55\% \le R\% \le 0.90\%$.
  8. **Conviction Ranker**: If multiple stocks signal on the same bar, only the **#1 ranked stock** is taken. Max **1 entry per bar** and **2 concurrent slots max**.
  9. **Slot 2 Lock**: If Slot 1 is open, Slot 2 remains locked until Slot 1 reaches at least **$+0.50R$ profit**.

---

### Step 4: Mid-Day Cutoff (12:30 IST)
- At **12:30 IST**, the engine blocks all new entries (`OUTSIDE_ENTRY_WINDOW`).
- This guarantees that every open position has at least **2 hours and 25 minutes** to reach its $+2.0R$ target before late-day square-off.

---

### Step 5: Late-Day Fee Protection Step (14:15 IST)
- If an open trade hits **$+1.65R$** at any time, or is at least **$+1.15R$** when 14:15 IST arrives:
- The engine modifies the broker stop-loss order to **`Entry Price ± 0.15%`**.
- This locks in government statutory taxes and broker commissions, eliminating the risk of a winning runner collapsing into a late-day loss.

---

### Step 6: Orderly MIS Square-Off (14:55 IST)
- At **14:55 IST**, the engine executes the orderly emergency exit sequence:
  - Cancels all pending SL-L protective orders.
  - Places market liquidation orders for all open MIS positions.
  - Flattens the account to **100% Cash** before Zerodha's automated RMS auto-square-off cutoff (15:15–15:20 IST), completely eliminating Zerodha square-off penalty charges (₹59/order).

---

### Step 7: Post-Market Daily Summary (15:00 IST)
- At **15:00 IST**, the dashboard and journal calculate the final session metrics:
  - Total completed trades.
  - Itemized Zerodha and statutory charges breakdown.
  - Net Realized P&L.
- All trade logs are permanently recorded in `apex3_journal.db`.

---

## 5. Emergency Controls & Safety Overrides

If you ever need to manually intervene during market hours, you have two instant controls on your dashboard (`http://localhost:5000` or via your Cloudflare URL):

1. **Pause / Resume Button**:
   - Freezes new entries immediately while continuing to protect and trail existing open positions.
2. **Emergency Flatten Button**:
   - Instantly cancels all pending protective orders and liquidates all open MIS positions at current market prices.
3. **Hard Daily Loss Stop (-₹240.00)**:
   - Built into the code. If two maximum allowable losses occur (-₹120 each, totaling 1.6% of ₹15,000 equity), the engine **automatically halts all trading for the rest of the day** and refuses to take new trades.

---

## 6. Cloud Management Quick Reference (GCP Terminal)

To monitor or manage the engine on your Google Cloud server:

```bash
# View live scrolling strategy logs
sudo docker compose logs -f

# Check container health status
sudo docker compose ps

# Restart the engine if needed
sudo docker compose restart

# Stop the engine
sudo docker compose down
```
