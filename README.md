# ORBot

Automated execution of the `SVMKR_UT_HMA_ORB` TradingView indicator on
[Dhan](https://dhan.co), across index options, futures and cash equity.

**Status: Phases 0–1 complete.** Data layer works end to end. No orders are
placed yet, and nothing trades until the indicator port clears the validation
gate in [PLAN.md](PLAN.md#6-the-validation-gate--most-important-section).

---

## The core idea

We do **not** read the TradingView chart. We recompute the indicator from Dhan's
own tick feed.

| | TradingView webhook | Recompute locally (this repo) |
|---|---|---|
| Latency | 1–3 s | ~50 ms, tick-level |
| Cost | $15/mo (Essential + 2FA) | free |
| Fidelity | perfect by construction | must be *proven* — see the validation gate |

Screen-scraping the chart was considered and rejected: 5–15 s lag, breaks on any
UI change, no reliable bar timestamps, and no way to tell a fresh signal from a
stale one on screen.

The second design decision that carries the whole project: **the signal
instrument and the execution instrument are decoupled.** The indicator runs on
the NIFTY index; the order goes to a 24500 CE. That one split is what lets a
single engine trade options, futures and equities from config alone.

## Quick start

```bash
pip install -e ".[dev]"
cp .env.example .env          # fill in Dhan credentials
pytest -q                     # 124 tests, no credentials needed

orbot doctor                  # config, credentials, connectivity
orbot instruments             # download today's scrip master
orbot resolve NIFTY           # look up a security id
orbot chain NIFTY --spot 24300   # verify strike + expiry selection
orbot warmup nifty-atm-ce     # historical candles — compare against the Dhan chart
orbot feed nifty-atm-ce       # live ticks, bars printed as they close
orbot halt                    # arm the kill switch
```

## Layout

```
src/orbot/
  broker/     Dhan REST, live feed + binary parser, instrument registry
  engine/     candle aggregation, indicators, strategy
  execution/  resolver, risk guards, OMS       (Phase 3)
  ingest/     TradingView webhook, replay      (Phase 6)
  ops/        Telegram, kill switch, health    (Phase 4)
config/
  instruments.yaml   trade plans — what to trade
  risk.yaml          account-wide limits
```

## Two things that will bite you

**Static IP.** Dhan requires a whitelisted static IP for order placement,
modification and cancellation. Data APIs do not. A Cloudflare Tunnel does *not*
satisfy this — a tunnel handles inbound traffic, whitelisting is about your
outbound source IP. Order calls are routed through a configured proxy
(`DHAN_ORDER_PROXY`); the market-data path deliberately goes direct.

**Repaint.** The UT Bot ATR trailing stop flips intrabar. `signal_on_bar_close`
defaults to true. Intrabar signals are available and will produce entries that
vanish before the candle closes.

## Testing

124 tests, of which roughly half are adversarial. The threat model is not an
attacker, it's a bad afternoon: a truncated frame during a reconnect, a NaN from
a stale contract, a volume counter reset at midnight, a CSV that changed shape
overnight. Every one of those can produce a *plausible* wrong number that gets
traded on.

The adversarial suite has already caught three real bugs — a malformed FULL
packet layout, non-finite prices reaching the candle engine, and a NaN spot
silently selecting a real-but-wrong strike.

```bash
pytest tests/test_adversarial.py -q
```

## Regulatory

SEBI's retail algo framework has been in force since 1 April 2026. Strategies
routed through broker APIs need registration with an exchange-approved strategy
ID, a static IP registered with the broker, and OAuth + 2FA per session. The
under-10-orders-per-second carve-out comfortably covers a 5-minute ORB strategy,
but the static-IP and auth requirements still apply. Confirm your account's
standing with Dhan's API desk before going live.

See [PLAN.md](PLAN.md) for the full design.
