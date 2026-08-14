# Does SVMKR_UT_HMA_ORB have a tradeable edge?

> **SUPERSEDED IN PART — read [FINDINGS-2026-08-15.md](FINDINGS-2026-08-15.md) first.**
>
> Several gold figures below were computed on a contract that was illiquid for
> part of the window, and are overstated. In particular the GOLD 5m two-week
> result and the "2h works" conclusion both need the corrections in that
> document. The silver results and the stop-loss analysis are only there.

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


## Position sizing on Rs 2,00,000 — the part that decides everything

Every P&L above was quoted per lot without stating the capital a lot requires.
That makes the numbers useless and, worse, flattering. Corrected:

| contract | size | notional | margin | affordable on Rs 2 lakh |
|---|---|---|---|---|
| GOLD | 1 kg | Rs 1.54 cr | Rs 9,24,000 | **no** |
| GOLDM | 100 g | Rs 15.3 lakh | Rs 92,400 | 1 lot safely, 2 not |
| GOLDGUINEA | 8 g | Rs 1.23 lakh | Rs 7,392 | 27 lots |

Running the equity curve against a live margin requirement, GOLDM 2h long only,
13 months:

| | final equity | worst drawdown | outcome |
|---|---|---|---|
| **1 lot** | Rs 3,99,430 (**+100%**) | Rs 33,259 | survives |
| 2 lots | Rs 6,01,927 (+201%) | Rs 65,998 | **margin call, 29 Oct 2025** |

The two-lot version reaches +201% on paper and never gets there in practice: it
falls below the margin requirement in October 2025 and is liquidated. Backtests
that ignore margin routinely report returns their own drawdowns would have
prevented.

### Where the return actually comes from

Leverage is **16.5 : 1**. A 6.0% adverse move wipes one lot's margin. Gold rose
58% across the window, so a long-only strategy participating even partially,
levered 16x, produces a large number. That is leverage on a bull market, not
alpha, and the same leverage runs in reverse.

The strategy's real contribution is survivability, and it is substantial: buying
and holding one GOLDM lot would have returned more in absolute terms but carried
a Rs 4,70,370 drawdown, which liquidates a Rs 2 lakh account several times over.
The strategy's worst drawdown was Rs 33,259 — 17% of capital.

### The risk that is not in any of these numbers

The largest overnight gap in the window was 4,283 points, **Rs 42,830 per lot —
46% of one lot's margin**. Daily square-off avoids gaps only while the bot is
actually running. A crash, a dropped network, an expired token or a power cut
during the 09:00-23:30 session leaves a levered position open into that gap.

That is the single strongest argument for the guard layer, the kill switch and
the two-week paper run — not for the strategy.


## The downtrend test — 7.5 years of daily gold

Angel serves daily bars back to January 2019 on GOLDM, so this covers 1,869
sessions and gold moving from Rs 31,950 to Rs 152,843.

| side | trades | win rate | net (1 lot) | max DD |
|---|---|---|---|---|
| long | 44 | 47.7% | **+Rs 5,31,728** | Rs 1,19,890 |
| short | 45 | 26.7% | −Rs 5,95,116 | Rs 6,80,720 |
| both | 89 | 37.1% | −Rs 63,388 | Rs 3,83,230 |

Long is profitable in **7 of 8 years**, losing only 2026 (−Rs 60,536). Short
loses in 6 of 8. Trading both sides is net negative because the short leg
destroys what the long leg earns — the same conclusion the 5-minute tests
reached, now over seven years.

On a Rs 2,00,000 account with 1 lot: final equity **Rs 7,32,330, +266%**, and it
never breached the margin requirement. Buy-and-hold returned more absolutely
(+Rs 12,08,930) with a Rs 3,18,470 drawdown that liquidates the account outright.

### The question this did not answer

**Gold rose in 7 of the 8 years, 378% in total. The only down year, 2021, fell
4%.** There is no real gold bear market anywhere in the available data, so
"does this survive a downtrend?" remains unanswered. What has been shown is that
a leveraged long-only trend follower makes money in the largest gold bull run in
decades, which is a much weaker claim.

### Sequence risk

The worst drawdown was Rs 1,20,473 — **60% of the starting capital**. It survived
only because it arrived after gains had accumulated. The same drawdown in year
one would have taken equity to Rs 79,527, below the Rs 92,400 margin, and the
position would have been liquidated. Survival here is partly an accident of
ordering, not a property of the strategy.

## Bottom line

Long only, GOLDM, one lot. Not two — two hits a margin call. Not the 1 kg GOLD
contract — it needs Rs 9.24 lakh of margin. Not the short side — it loses on
every instrument and timeframe tested. Not NIFTY options — the signal is
inverted there and theta compounds the damage.

And not with confidence that it works when gold falls, because nothing in the
data says whether it does.
