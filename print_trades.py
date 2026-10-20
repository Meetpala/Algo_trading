import json

with open('all_trades.json') as f:
    data = json.load(f)

p = data['primary_allocated']
print(f"Total Trades: {len(p['trades'])}")
for i, t in enumerate(p['trades'], 1):
    c = t['charges']
    print(f"{i:2d} | {t['date']} | {t['symbol']:8s} | {t['side']:4s} | Qty:{t['qty']:3d} | In: {t['entry_time']} @ {t['entry_price']:7.2f} | Out: {t['exit_time']} @ {t['exit_price']:7.2f} | {t['exit_reason']:20s} | Gross: {t['gross_pnl']:+7.2f} | Fee: {c['total_charges']:5.2f} | Net: {t['net_pnl']:+7.2f}")
