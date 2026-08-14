# ORBot — SVMKR_UT_HMA_ORB Automated Execution System

End-to-end design. Read this fully before implementation starts.

---

## 1. Executive summary

Replicate the `SVMKR_UT_HMA_ORB` TradingView indicator in Python, drive it from Dhan's
free tick-level WebSocket feed, and execute the resulting BUY/SELL signals on Dhan
automatically across equities, futures and options — behind a hard risk-guard layer.

**Core decision: we do NOT read the TradingView chart.** We recompute the indicator.

| | Read TradingView (webhook) | Recompute locally (CHOSEN) |
|---|---|---|
| Latency | 1–3 s | ~50 ms (tick-level) |
| Cost | $15/mo (Essential + 2FA) | Free |
| Signal fidelity | Perfect by construction | Must be *proven* by validation |
| Failure modes | TV outage, no delivery SLA | Our bug, feed drop |
| Granularity | Bar close only | Tick or bar close, configurable |

The recompute path wins on every axis except fidelity — and fidelity is buyable with
a rigorous validation gate (§6). The webhook path is still built, as an optional
second ingest source and as a cross-check oracle during validation.

**Vision / screen-scraping is explicitly rejected.** 5–15 s lag, breaks on any UI
change, no reliable bar timestamps, cannot distinguish a fresh signal from a stale one
on screen, dies when the machine sleeps. It is the obvious approach and the wrong one.

---

## 2. Architecture

```
                    ┌───────────────── SIGNAL SOURCES ─────────────────┐
                    │                                                  │
  Dhan WebSocket ───┤  PRIMARY: tick feed → candle builder → indicator │
  (free, tick-level)│           → SVMKR_UT_HMA_ORB → Signal            │
                    │                                                  │
  TradingView ──────┤  OPTIONAL: alert → webhook → Signal              │
  (if Essential)    │  (Cloudflare Tunnel inbound)                     │
                    └───────────────────────┬──────────────────────────┘
                                            │  Signal{plan, action, price, bar_ts}
                                            ▼
                    ┌──────────────────────────────────────────────────┐
                    │  GUARD LAYER — every signal passes all 14 checks  │
                    │  kill switch · daily loss cap · dedupe · staleness │
                    │  market hours · funds · rate limit · fat-finger …  │
                    └───────────────────────┬──────────────────────────┘
                                            ▼
                    ┌──────────────────────────────────────────────────┐
                    │  INSTRUMENT RESOLVER                              │
                    │  signal instrument  →  execution instrument       │
                    │  NIFTY index BUY → NIFTY 25AUG 24500 CE, 1 lot    │
                    └───────────────────────┬──────────────────────────┘
                                            ▼
                    ┌──────────────────────────────────────────────────┐
                    │  OMS — place · poll · reconcile · partial fills    │
                    │  ── via Decodo STATIC IP proxy (Dhan requirement) │
                    └───────────────────────┬──────────────────────────┘
                                            ▼
                        SQLite journal  ·  Telegram alerts  ·  structlog
```

**Key architectural insight:** the *signal instrument* and the *execution instrument*
are decoupled. That single abstraction is what delivers "supports all types" — index
options, stocks, futures, F&O — from one engine with different config blocks.

---

## 3. Repository layout

```
e:\Trade\
  PLAN.md                    this document
  .env                       secrets (gitignored)
  config/
    instruments.yaml         trade plans — the "what to trade" config
    risk.yaml                global risk limits
    calendar.yaml            NSE holidays, special sessions
  data/
    scrip_master.csv         refreshed daily 08:00
    journal.db               SQLite audit trail
    warmup/                  cached historical candles
  src/orbot/
    config.py                pydantic settings, env + yaml
    logging.py               structlog JSON
    broker/
      dhan_client.py         REST: orders, positions, funds, historical
      dhan_feed.py           WebSocket + binary packet parser (little-endian)
      instruments.py         scrip master → registry, symbol resolution
      models.py              Order · Position · Tick · Candle
    engine/
      candles.py             tick → OHLCV aggregator + resampler
      indicators/
        atr.py               Wilder RMA ATR  (matches Pine ta.atr)
        hma.py               Hull MA         (matches Pine ta.hma)
        heikinashi.py
        utbot.py             ATR trailing-stop state machine
        orb.py               opening range breakout
      strategy.py            composite → Signal
      state.py               per-instrument state, persisted across restarts
    execution/
      resolver.py            strike picker · expiry picker · futures roll
      guards.py              THE RISK LAYER
      oms.py                 place · poll · reconcile · retry · partials
      positions.py           local book, reconciled against broker
    ingest/
      webhook.py             FastAPI, TradingView path (optional)
      replay.py              historical replay — validation + backtest
    ops/
      notifier.py            Telegram
      killswitch.py
      health.py              heartbeat + feed-staleness watchdog
    cli.py                   typer: run · validate · replay · paper · status
  tests/
    test_indicators_vs_pine.py   golden-file comparison
    test_guards.py
    test_resolver.py
```

---

## 4. Data layer (Dhan, all free)

**Live feed** — `wss://api-feed.dhan.co`, 5 connections × 5000 instruments each.
JSON requests, **binary little-endian responses**. Packet types: LTP, Quote (+OI),
Full (LTP + Quote + OI + market depth). We use Quote for signal instruments.

**Historical** — 1/5/15/25/60 min candles, 5 years back, max 90 days per call.
No rate limit on minute/hourly frames. Used for indicator warmup and replay validation.

**Instruments** — `https://images.dhan.co/api-data/api-scrip-master-detailed.csv`,
refreshed daily. Maps tradingsymbol ↔ securityId ↔ exchangeSegment ↔ lot size ↔
expiry ↔ strike. This is what makes multi-instrument support tractable.

**Rate limits** — orders: 25/sec, 7000/day. Data: 100k/day. We cap far below both.

---

## 5. Indicator port — Pine → Python

The three components, and how Pine computes them:

**UT Bot** — ATR trailing stop. `nLoss = keyValue * ta.atr(atrPeriod)`. The stop
ratchets up in an uptrend, down in a downtrend, and flips on cross. Buy when price
crosses above the stop and `ema(close,1) > stop`. Optional Heikin Ashi source.

**HMA** — `wma(2*wma(src, n/2) - wma(src, n), floor(sqrt(n)))`. Colour flips on slope.

**ORB** — high/low of a defined opening window (typically 09:15–09:30 IST); a breakout
of that range confirms direction.

### Gotchas that will silently break fidelity

1. **ATR must be Wilder's RMA**, not SMA. `pandas.rolling().mean()` is wrong.
   `rma[i] = (rma[i-1]*(n-1) + tr[i]) / n`.
2. **HMA needs WMA**, not SMA or EMA, at all three stages.
3. **Warmup** — need roughly 3× the longest period of prior bars before the first
   valid signal. Seed from the historical API, never from a cold live start.
4. **Repaint** — the UT Bot stop flips intrabar. Bar-close-only is the default and the
   safe choice; intrabar is configurable but will produce ghost signals.
5. **Timezone** — TradingView uses exchange time (Asia/Kolkata for NSE). All session
   logic in IST, DST-free but holiday-aware.
6. **`na` / `nz()` semantics** — Pine's null handling on early bars differs from numpy
   NaN propagation. Port explicitly.

---

## 6. THE VALIDATION GATE — most important section

If our Python signals differ from the chart, everything downstream is worthless.
This gate is where the extra time gets spent, and it is not skippable.

1. Obtain the exact Pine source of `SVMKR_UT_HMA_ORB` (**blocker — needed from you**).
2. Port line-by-line with the §5 gotchas handled.
3. Pull 90 days of 1-min history from Dhan for each signal instrument.
4. Replay through the engine → emit a timestamped signal list.
5. On TradingView, apply the same indicator over the same range; export or spot-check
   the signal bars.
6. **Gate: ≥200 signals compared, 100% match on direction and bar timestamp.**
   Anything less and we do not go live — we debug.
7. Optional: if you hold TradingView Essential during this phase only, run the webhook
   path in parallel as a live oracle for a week, then cancel.

---

## 7. Trade plans — how "all instrument types" works

`config/instruments.yaml`:

```yaml
plans:
  - name: nifty-weekly-atm
    enabled: true
    signal:
      exchange_segment: IDX_I
      security_id: "13"            # NIFTY 50 index
      timeframe: 5m
    strategy:
      preset: svmkr_ut_hma_orb
      params:
        ut_key: 1.0
        ut_atr_period: 10
        hma_length: 55
        orb_start: "09:15"
        orb_end:   "09:30"
    execution:
      mode: option                 # option | same | future
      underlying: NIFTY
      on_buy:  {option_type: CE, strike: ATM, expiry: nearest_weekly}
      on_sell: {action: exit}      # or mirror into PE — see open question 2
      product: INTRADAY
      order_type: MARKET
      qty_lots: 1
    risk:
      sl_pct: 25
      target_pct: 50
      max_trades_per_day: 4
      square_off_time: "15:10"

  - name: reliance-cash
    signal:    {exchange_segment: NSE_EQ, security_id: "2885", timeframe: 15m}
    execution: {mode: same, product: INTRADAY, qty: 50}
    risk:      {sl_pct: 1.5, max_trades_per_day: 2, square_off_time: "15:15"}
```

Same engine. Different `execution` block. That is the whole multi-asset story.

---

## 8. Guard layer — all mandatory

| # | Guard | Trip action |
|---|---|---|
| 1 | Kill switch (`HALT` file or Telegram `/halt`) | block all new orders |
| 2 | Daily loss cap (realised + unrealised) | halt + flatten |
| 3 | Max trades/day per plan | block plan |
| 4 | Max open positions, global | block new |
| 5 | Duplicate suppression — idempotency key `(plan, bar_ts, action)` | drop signal |
| 6 | **Feed staleness** — no tick in N sec during market hours | halt (never trade stale data) |
| 7 | Market hours + NSE holiday calendar | drop signal |
| 8 | Available funds check | block order |
| 9 | Order rate limiter (well under Dhan's 25/s, 7000/day) | queue or drop |
| 10 | Fat-finger — qty and notional bounds | block + alert |
| 11 | Position reconciliation vs broker every N sec | mismatch → halt + alert |
| 12 | Auto square-off before close | force exit |
| 13 | Slippage cap on LIMIT / market protection band | reject |
| 14 | Signal staleness — `bar_ts` older than N sec | drop signal |

Guards 5, 6 and 11 are the ones that prevent the classic unattended-bot disasters:
duplicate signal retries, trading on a frozen feed, and silent position drift.

---

## 9. Static IP — hard requirement

Dhan's **order placement APIs require static IP whitelisting**. Data APIs do not.

- Cloudflare Tunnel does **not** satisfy this. Tunnel = inbound. Whitelist = outbound.
- Decodo must provide a **dedicated static ISP/datacenter IP**, not a rotating pool.
  *Confirm this with Decodo before implementation.*
- Design: the proxy is applied **per-client**, not globally.
  `DhanClient(proxy=DECODO)` for REST orders; `DhanFeed()` direct — routing the
  WebSocket through a proxy adds latency and instability for no compliance benefit.
- Cost: ~20–80 ms added per order call. Irrelevant at 5-minute bars.
- **Single point of failure:** proxy down ⇒ cannot place *or exit* orders. Needs a loud
  alert and a documented manual-exit runbook. Named here deliberately.

---

## 10. Regulatory (SEBI, live since 1 April 2026)

- Retail API-routed strategies must be registered via the broker with an
  exchange-approved strategy ID.
- Static IP registered with the broker — already required by Dhan regardless.
- OAuth + 2FA per API session.
- **Carve-out: under 10 orders/second per exchange needs no algo registration.**
  A 5-minute ORB strategy is orders of magnitude below this, so frequency is a
  non-issue — but the static-IP and auth requirements still apply.
- Action: one call to Dhan's API desk before going live to confirm your account's
  standing. Five minutes, avoids a nasty surprise.

---

## 11. Delivery phases

| Phase | Scope | Exit criteria |
|---|---|---|
| **0 — Foundation** | repo, config, Dhan auth, scrip master, connectivity smoke test | can fetch funds + resolve NIFTY securityId |
| **1 — Data** | WebSocket + binary parser, tick→candle, historical warmup | live 5m candles match the Dhan chart exactly |
| **2 — Indicator** | Pine port + **validation gate §6** | ≥200 signals, 100% match vs TradingView |
| **3 — Execution** | resolver, OMS, guards, paper mode | paper trades end-to-end, all 14 guards unit-tested |
| **4 — Paper run** | 2 weeks unattended paper trading | zero crashes, zero guard false-positives, P&L tracks chart |
| **5 — Live** | static IP, Telegram, small size | first live trade, 1 lot |
| **6 — Webhook** | optional TradingView ingest path | parallel signal source working |

Phases 0–3 and 5–6 are engineering sessions. Phase 4 is calendar time and cannot be
compressed — it is what separates a working script from a system you can leave alone.

---

## 12. Daily operating cycle

```
08:00  refresh scrip master, refresh holiday calendar
09:00  warmup — pull historical candles, seed indicator state
09:10  connect WebSocket, verify feed heartbeat
09:15  ORB window opens — trading enabled
15:10  auto square-off (per-plan configurable)
15:30  EOD reconciliation, journal flush, Telegram P&L report
```

Runs on Windows as an NSSM service or scheduled task with auto-restart.
Crash ⇒ immediate Telegram alert. Heartbeat every 30 min while the market is open.

---

## 13. Open questions — needed before Phase 2

1. **Pine source of `SVMKR_UT_HMA_ORB`** — exact version, from TradingView's Pine
   Editor. **Hard blocker.** Without it the port is guesswork.
2. **What does SELL mean?** Exit the long only, or reverse into a short / buy a PE?
   This materially changes the resolver and the risk profile.
3. Dhan `client_id` + `access_token` (web.dhan.co → Profile → DhanHQ Trading APIs).
4. Decodo static IP + proxy credentials, and confirmation it is dedicated and static.
5. Telegram bot token + chat ID (via @BotFather).
6. First instruments and timeframes to run.
7. Capital per trade, lot-sizing rule, and the daily loss cap number.
