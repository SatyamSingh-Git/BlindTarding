# ORBot

Automated execution of the `SVMKR_UT_HMA_ORB` TradingView indicator on
[Dhan](https://dhan.co), across index options, futures and cash equity.

**Status: Phases 0–2 complete.** Data layer and indicator port both work. No
orders are placed yet, and nothing trades until the port clears the validation
gate in [PLAN.md](PLAN.md#6-the-validation-gate--most-important-section).

---

## The finding that matters most

**The indicator's name is misleading.** Tracing the [Pine
source](docs/SVMKR_UT_HMA_ORB.pine), `buy` and `sell` depend only on the UT Bot
ATR trailing stop:

```pine
buy  = src > xATRTrailingStop and crossover(ema(src,1), xATRTrailingStop)
sell = src < xATRTrailingStop and crossover(xATRTrailingStop, ema(src,1))
```

`n1` (the Hull MA) appears only in a colour expression and a `plot()`.
`orb_high` / `orb_low` appear only in their own computation and two `plot()`
calls. Neither is referenced by `buy`, `sell`, or either `alertcondition`. The
UCS_LRS block is commented out entirely.

So every Buy/Sell label on the chart is **pure UT Bot** — the Hull MA and the
opening range are visual context for a human, not filters. Both are ported and
available as optional filters, defaulting to off so the engine reproduces the
chart exactly. Regression tests assert that changing `hma_length` or
`orb_session` moves *zero* signals.

Also worth knowing: the live chart runs **UT Key Value 3**, where the Pine
source defaults to 2. And the ORB session is **10:10–10:15**, not the market
open, despite the name.

## Design

We do **not** read the TradingView chart. We recompute the indicator from
Dhan's own tick feed.

| | TradingView webhook | Recompute locally (this repo) |
|---|---|---|
| Latency | 1–3 s | ~50 ms, tick-level |
| Cost | TradingView Essential ~₹1,300/mo | Dhan Data API ₹499 + GST/mo |
| Historical data | not included | included |
| Fidelity | perfect by construction | must be *proven* — see the validation gate |

Note the two Dhan products are easy to confuse: **Trading** APIs (orders,
positions, funds) are free; **Data** APIs (live feed, quotes, historical) are
₹499 + GST/month and are what this architecture runs on. Live prices also
require an exchange-registered account — a freshly opened one shows 15-minute
delayed data until KYC and exchange registration complete, 1–2 business days
after document approval.

Screen-scraping the chart was considered and rejected: 5–15 s lag, breaks on
any UI change, no reliable bar timestamps, no way to tell a fresh signal from a
stale one on screen.

The second decision that carries the project: **the signal instrument and the
execution instrument are decoupled.** The indicator runs on the NIFTY index;
the order goes to a 24500 CE. That split is what lets one engine trade options,
futures and equities from config alone.

## Quick start

```bash
pip install -e ".[dev]"
cp .env.example .env          # fill in Dhan credentials
pytest -q                     # 244 tests, no credentials needed

orbot doctor                  # config, credentials, connectivity
orbot instruments             # download today's scrip master
orbot timeframes              # what the engine can serve, and from where
orbot resolve NIFTY           # look up a security id
orbot chain NIFTY --spot 24300      # verify strike + expiry selection
orbot warmup  nifty-atm-ce -t 15m   # historical bars — compare against the chart
orbot signals nifty-atm-ce -t 5m    # replay history, list every signal
orbot feed    nifty-atm-ce          # live ticks, bars printed as they close
orbot halt                    # arm the kill switch
```

## Timeframes

Selectable at runtime, 5 minutes to 2 days, via `--timeframe` / `-t` on any
data command or `signal.timeframe` in a plan.

Dhan serves only 1/5/15/25/60-minute and daily natively. Everything else is
built by resampling, and **only native intervals can be validated against the
chart directly** — resampled ones have bar boundaries we chose rather than the
exchange, so they need checking separately. `orbot timeframes` marks which is
which, and every command warns when you pick a resampled one.

Resampling buckets by session-anchored wall clock, never by counting bars. An
NSE session is 375 minutes, which divides evenly by 5, 15 and 25 but not by 30
or 60 — count-based grouping drifts a fraction of a bar per day and eventually
emits a bar spanning an overnight gap.

## Layout

```
src/orbot/
  broker/     Dhan REST, live feed + binary parser, instrument registry
  engine/     timeframes, candle aggregation, indicators, strategy
    indicators/pine.py    Pine v4 primitives — where fidelity lives
  execution/  resolver, risk guards, OMS       (Phase 3)
  ingest/     TradingView webhook, replay      (Phase 6)
  ops/        Telegram, kill switch, health    (Phase 4)
config/
  instruments.yaml   trade plans — what to trade
  risk.yaml          account-wide limits
docs/
  SVMKR_UT_HMA_ORB.pine   the source, verbatim
```

## Two things that will bite you

**Static IP.** Dhan requires a whitelisted static IP for order placement,
modification and cancellation. Data APIs do not. A Cloudflare Tunnel does *not*
satisfy this — a tunnel handles inbound traffic, whitelisting is about your
outbound source IP. Order calls route through `DHAN_ORDER_PROXY`; the
market-data path deliberately goes direct.

**Repaint.** The UT Bot stop flips intrabar, and at `ATR period 1` it reacts to
a single bar. `signal_on_bar_close` defaults to true. Intrabar signals are
available and will produce entries that vanish before the candle closes.

## Testing

244 tests. Roughly half are adversarial — the threat model is not an attacker,
it's a bad afternoon: a truncated frame during a reconnect, a NaN from a stale
contract, a volume counter reset at midnight, a CSV that changed shape
overnight. Each of those can produce a *plausible* wrong number that gets
traded on.

The adversarial suite has caught four real bugs so far: a malformed FULL packet
layout, non-finite prices reaching the candle engine, a NaN spot silently
selecting a real-but-wrong strike, and Dhan's `0001-01-01` null-expiry sentinel
sorting ahead of every genuine expiry.

```bash
pytest tests/test_adversarial.py -q
```

Pine-fidelity tests live in `tests/test_indicators.py`. They target the specific
ways a naive translation diverges: `round()` is half-away-from-zero not
banker's, `atr()` is Wilder RMA not a rolling mean, `crossover` tests `<=` on
the previous bar, and `na` comparisons are false rather than NaN-propagating.

## Regulatory

SEBI's retail algo framework has been in force since 1 April 2026. Strategies
routed through broker APIs need registration with an exchange-approved strategy
ID, a static IP registered with the broker, and OAuth + 2FA per session. The
under-10-orders-per-second carve-out comfortably covers this strategy, but the
static-IP and auth requirements still apply. Confirm your account's standing
with Dhan's API desk before going live.

See [PLAN.md](PLAN.md) for the full design.
