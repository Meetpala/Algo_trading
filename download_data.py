import os
import yfinance as yf

symbols = [
    '^NSEI',
    'RELIANCE.NS',
    'TCS.NS',
    'INFY.NS',
    'HDFCBANK.NS',
    'ICICIBANK.NS',
    'BHARTIARTL.NS',
    'ITC.NS',
    'SBIN.NS',
    'HINDUNILVR.NS',
    'LT.NS',
    'KOTAKBANK.NS',
    'BAJFINANCE.NS',
    'AXISBANK.NS',
    'MARUTI.NS',
    'ASIANPAINT.NS'
]

os.makedirs('scratch_data', exist_ok=True)
for s in symbols:
    clean_name = s.replace('^', '').replace('.NS', '')
    fname = os.path.join('scratch_data', f'{clean_name}.csv')
    print(f'Downloading {s} -> {fname}...')
    df = yf.download(s, period='60d', interval='5m', progress=False)
    if not df.empty:
        if hasattr(df.columns, 'levels') and len(df.columns.levels) > 1:
            df.columns = df.columns.get_level_values(0)
        df.to_csv(fname)
        print(f'Saved {clean_name}: {len(df)} rows')
    else:
        print(f'Failed {s}: empty')
