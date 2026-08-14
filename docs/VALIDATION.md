# Validation gate — result

**Status: the Pine port is validated. The data source is not.**

Compared against TradingView, `NSE:NIFTY` 5-minute, indicator settings
`3 / 1 / 31 / 5 / 1010-1015`, on 2026-08-14.

## Proof 1 — plot values match exactly at an arbitrary bar

At the crosshair `Thu 13 Aug '26 14:20`, a bar with no signal on it:

| Plot | TradingView | Engine |
|---|---|---|
| Buy | 0.00 | 0.00 |
| Sell | 0.00 | 0.00 |
| HullMA | 24,366.88 | 24,366.88 |
| ORB High | 24,350.30 | 24,350.30 |
| ORB Low | 24,330.30 | 24,330.30 |

The Hull MA is the demanding one. Reproducing it to the paisa requires
`round(31/2)=16` and `round(sqrt(31))=6` under Pine's half-away-from-zero
rounding, correct WMA weighting at all three stages, and identical bar
alignment. One error anywhere gives a different number.

## Proof 2 — 96.3% of signals are insensitive to the known data gaps

Over 90 days / 63 sessions: 214 signals, 206 identical whether or not the
missing bars are filled.

## Proof 3 — the single real disagreement is explained by missing data

TradingView fires SELL at 13 Aug 09:30; the engine fires at 10:25.

Cause: Angel's historical API omits 12 Aug 15:15 and 15:20. The UT Bot stop
ratchets as `max(prev_stop, close - 3*ATR)`, so a bar we never receive is a
ratchet that never happens. Our stop ends 12 Aug at 24,321.55, which is 2.80
points below what TradingView carries. At 13 Aug 09:30 the close of 24,324.35
sits between the two — below TradingView's stop, above ours.

Synthesising a plausible bar for the gap reproduces the 09:30 SELL exactly,
confirming the cause.

## The gaps

Angel's historical data for the NIFTY index systematically omits the 15:20 bar,
and occasionally 15:15. Present in 10 of 63 sessions. **The 1-minute data has
the same gaps**, so this is missing at source, not an aggregation artifact —
building 5m from 1m recovers nothing.

## Impact

Of the 8 changed signals, 7 are `15:20` versus `15:25` — both past the 15:10
square-off, so guard 12 blocks them either way and there is no trading impact.
**One signal in 63 sessions would have been traded differently.**

The divergence is also self-limiting rather than cumulative: when price next
crosses the stop it resets to `close +/- 3*ATR`, and both systems re-sync.

## Deliberately not fixed by interpolation

Filling gaps by interpolation matches TradingView more often in this sample,
and is still the wrong call: it invents prices that never traded. Here the guess
happened to be good; a bad guess produces a wrong signal just as easily and
leaves no trace. A visible gap is better than invented data.

`orbot signals` reports gaps and flat bars before printing any signal, so a
divergence caused by data cannot be mistaken for one caused by the port.

## Open — test during market hours

The gaps are in the historical API. The live feed builds candles from ticks, and
the market trades at 15:15-15:20, so a continuously-running bot should have
complete candles and only a cold warmup would carry gappy history. If so this is
self-healing across sessions.

    orbot feed nifty-atm-ce -s 300     # run across 15:15-15:25

If bars print at 15:15 and 15:20, the live path is clean.
