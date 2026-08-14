# Does SVMKR_UT_HMA_ORB have a tradeable edge?

Tested across three instruments and four timeframes, on real broker data, with
the chart-configured parameters (`ut_key 3, ATR 1, HMA 31`).

**Conclusion: at 5 minutes, no — on any instrument. At 2 hours on MCX GOLD,
yes, on a risk-adjusted basis.**

The pattern across everything tested: the signal earns too little per trade to
clear transaction costs at fast intervals, and only becomes viable where moves
are large enough that costs stop dominating.

## Summary

| Instrument | TF | Sessions | Trades | Gross | After costs | Verdict |
|---|---|---|---|---|---|---|
| NIFTY (options) | 5m | 35 | 49 | −382 pts | **−Rs 9,441** real | loses |
| USDINR future | 5m | 43 | 135 | −0.41 | worse | loses |
| GOLD future | 5m | 49 | 229 | +5,773 pts | **−1,189 pts** | loses |
| GOLD future | 1h | 279 | 102 | +25,475 pts | +22,374 pts | beaten by buy-and-hold |
| **GOLD future** | **2h** | **279** | **71** | **+15,705 pts** | **+Rs 13,93,888** | **works, risk-adjusted** |

## The 5-minute results

NIFTY long-only: 38.8% win rate, profit factor 0.62, in a market that **rose
966 points**. Buy-and-hold beat the strategy's long side by 1,348 points.
Modelled against real option premiums the win rate falls to 27% and the profit
factor to 0.43, because theta is paid on every trade regardless of direction.

USDINR long-only: 38.5% win rate. Near-identical to NIFTY's 38.8% on a
completely unrelated instrument, which points at the signal rather than at
either market. The short side, which looked profitable on NIFTY, collapses to
profit factor 0.53 here — that earlier result was regime, not edge.

GOLD 5m looked promising at profit factor 1.19 gross, and **loses after costs**.
The strategy earns 25.2 points per trade; a round trip costs about 30.4, of
which CTT alone is 15.4. Switching to the mini contract does not help: CTT and
point value both scale with notional, so the cost *in points* is unchanged.

## The 1-hour result, which looks good and is not

GOLD 1h returns profit factor 1.91 over 102 trades, net +22,374 points after
costs, profitable in 5 of 5 quarters. Three things undo it:

**It underperforms doing nothing.** Gold rose from 97,626 to 154,115 over the
window — 58%. Buy-and-hold returned +56,489 points. The strategy returned
+22,374 while taking 102 trades' worth of costs and an 8,186-point drawdown
(Rs 818,600 per lot).

**74% of the gross profit comes from 5 trades** out of 102. Strip those and 97
trades produce +6,598 gross, +3,649 net — a rounding error against the
drawdown required to earn it.

**The timeframe pattern is not monotonic.** 5m wins gross, 15m loses, 30m wins,
60m wins big. A real edge strengthens smoothly as costs shrink relative to trade
size. Zig-zagging is what noise looks like.

## MCX GOLD at 2 hours — the one that works

Long only, 279 sessions, 2h bars built from Angel's 60m, squared off on each
session's last bar so nothing carries overnight, costed with real MCX charges
including CTT.

| | net P&L | max drawdown | return/DD | exposure |
|---|---|---|---|---|
| strategy, long only | **+Rs 13,93,888** | Rs 3,45,200 | **4.04** | **7%** |
| buy and hold | +Rs 56,90,500 | Rs 47,03,700 | 1.21 | 100% |

Buy and hold makes four times more in absolute terms and carries **13.6x the
drawdown** to do it. Per unit of risk the strategy is 3.3x better, and it is
exposed to the market 7% of the time. Sized to the same drawdown, the strategy
would return roughly Rs 1.9 crore against buy-and-hold's Rs 57 lakh.

It also passes the robustness checks the other configurations failed:

* **Parameter robust.** Every `ut_key` from 1.0 to 8.0 is net profitable, from
  135 trades down to 26. Not a knife edge.
* **4 of 5 quarters profitable** after costs; 2026 Q2 lost Rs 2,49,300.
* **Costs are 6% of gross**, against 112% on RELIANCE 5m. Two-hour bars capture
  moves large enough that CTT stops mattering.

Two things keep this at "worth paper trading" rather than "trade it":

* **76% of the profit comes from 5 of 71 trades.** Strip them and it still nets
  +Rs 3,52,800 over the remaining 66 — it stays positive, which is more than
  most curve-fits manage, but the magnitude rides on outliers.
* **Gold rose 58% across the window** and this is long only. The short side
  loses Rs 6,37,211 over the same period, so there is no evidence the signal
  works when gold falls.

## What this cost to find out

Nothing, which was the point. The machinery answers this in an afternoon:
broker-agnostic data, an indicator port validated against TradingView to the
paisa, a backtest that models real option premiums and real costs, and a
dashboard that makes the sweep interactive.

## If the search continues

The bar any candidate must clear:

1. Profit factor above 1 **after costs**, not gross.
2. Profitable across **independent** periods — quarters, not one lucky window.
3. Beats buy-and-hold on the same instrument over the same window.
4. Not dependent on a handful of trades — check the top-5 concentration.
5. Robust to parameters — an edge that needs one exact `ut_key` is curve fit.

The 5-minute NIFTY configuration fails all five.

## The bug that nearly hid this

Early runs squared off at a fixed clock time. Where a session's last bar opened
before that time, nothing forced the exit and positions carried overnight. On
RELIANCE that dropped the win rate from 44.7% to 36.0% and turned +Rs 14,890
into +Rs 1,830; on MCX, whose session end shifts with daylight saving, it left
7 trades spanning days. Squaring off on each session's **last bar** rather than
a clock time fixes it everywhere.

It was caught because the chart looked more profitable than the numbers, and
the discrepancy turned out to be real rather than the usual visual bias.
