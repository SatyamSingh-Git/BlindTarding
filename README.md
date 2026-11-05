# ORBot

<div align="center">

### A safety-first automated execution engine for Indian markets

Recompute TradingView strategy signals locally, resolve instruments across
equities, futures, and options, and route orders through a guarded broker
adapter.

<p>
  <a href="#quick-start">Quick start</a> ·
  <a href="#architecture">Architecture</a> ·
  <a href="#risk-controls">Risk controls</a> ·
  <a href="#documentation">Documentation</a>
</p>

</div>

> **Important:** ORBot defaults to paper mode. It is not financial advice.
> Validate every strategy, broker configuration, and regulatory requirement
> before using real money.

## What ORBot does

ORBot is a Python execution system for the `SVMKR_UT_HMA_ORB` TradingView
indicator. It does **not** read or scrape the TradingView chart. Instead, it
rebuilds candles and indicators from broker data, emits signals locally, and
passes every order through a persistent risk-guard layer.

**No screen scraping. No browser automation. No unofficial trading clients.**

## Highlights

| Capability | Description |
| --- | --- |
| **Local signal engine** | Pine-compatible indicator primitives, candle aggregation, replay, and configurable bar-close behavior. |
| **Multi-broker adapters** | Dhan and Angel One SmartAPI behind one broker interface. |
| **Multi-instrument execution** | Decouple the signal instrument from the execution instrument for index options, futures, and equities. |
| **Persistent operations** | SQLite journal, position reconciliation, sticky halts, Telegram alerts, and structured logs. |
| **Paper mode** | Exercise the same strategy, guards, resolver, position book, and journal without sending live orders. |
| **Validation tooling** | Compare historical output, inspect native/resampled timeframes, and fail closed on invalid plans. |

## The important strategy finding

The indicator name is more ambitious than its signal logic. In the source Pine
script, `buy` and `sell` depend only on the UT Bot ATR trailing stop:

```pine
buy  = src > xATRTrailingStop and crossover(ema(src,1), xATRTrailingStop)
sell = src < xATRTrailingStop and crossover(xATRTrailingStop, ema(src,1))
```

The Hull moving average and opening-range values are plotted context, not signal
filters. ORBot ports them as optional filters, but they default to disabled so
the engine reproduces the chart's actual signal behavior. The original source
is preserved in [`docs/SVMKR_UT_HMA_ORB.pine`](./docs/SVMKR_UT_HMA_ORB.pine).

## Architecture

```text
Broker WebSocket / historical API
                │
                ▼
        candles + timeframes
                │
                ▼
      Pine-compatible indicators
                │
                ▼
              Signal
                │
                ▼
          14 risk guards
                │
                ▼
      instrument resolver
  signal instrument → execution instrument
                │
                ▼
       OMS + reconciliation
                │
                ▼
    SQLite journal · alerts · logs
```

The signal instrument and execution instrument are intentionally separate. For
example, a signal generated from the NIFTY index can resolve to a configured
NIFTY call option, future, or another supported instrument.

## Brokers

Select the adapter with `BROKER=dhan` or `BROKER=angel`.

| | Dhan | Angel One SmartAPI |
| --- | --- | --- |
| Trading API | Free | Free |
| Market data | Paid Data API | Free |
| Native intervals | 1, 5, 15, 25, 60 minutes | 1, 3, 5, 10, 15, 30, 60 minutes |
| Order idempotency | Correlation ID lookup | Recovery heuristic |
| Key warning | Security IDs are broker-specific | Tokens are broker-specific |

Switching brokers requires resolving every configured instrument again.
`orbot validate` rejects IDs belonging to the other broker.

## Risk controls

Every signal must pass the guard layer. The system fails closed when it cannot
prove that a trade is safe.

| Guard | Behavior |
| --- | --- |
| Kill switch | Blocks all new orders |
| Daily loss cap | Halts and can flatten positions |
| Plan and account limits | Caps trades and open positions |
| Persistent deduplication | Prevents a signal from firing twice after restart |
| Feed and signal staleness | Blocks stale or future-dated data |
| Market calendar | Respects market hours and holidays |
| Funds and sizing | Blocks insufficient funds and fat-finger quantities |
| Reconciliation | Halts when local and broker positions disagree |
| Square-off window | Blocks new entries near close |
| Slippage and rate limits | Rejects unsafe prices and excessive order volume |

Paper mode uses the same path as live mode except for the order-management
implementation. Paper fills are optimistic and are not a prediction of returns.

## Quick start

### Requirements

- Python 3.11+
- A Dhan or Angel One account for broker-connected commands
- Credentials configured through `.env` (never commit `.env`)
- A static IP or approved order proxy where the broker requires one

### Install

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
cp .env.example .env
```

### Verify the setup

```bash
pytest -q
orbot doctor
orbot validate
```

### Run safely in paper mode

```bash
orbot instruments
orbot timeframes
orbot warmup nifty-atm-ce -t 15m
orbot signals nifty-atm-ce -t 5m
orbot run --mode paper
orbot status
```

Useful operational commands:

```bash
orbot token       # inspect token lifetime and renewal setup
orbot resolve NIFTY
orbot chain NIFTY --spot 24300
orbot feed nifty-atm-ce
orbot halt
orbot flatten
```

## Configuration

Copy [`.env.example`](./.env.example) to `.env` and configure only the broker
you intend to use. Long-lived unattended operation requires the broker's
supported token-renewal setup. Keep API keys, PINs, TOTP secrets, access
tokens, Telegram tokens, and proxy credentials outside Git.

Trade plans live in:

- [`config/instruments.yaml`](./config/instruments.yaml) — signal and execution instruments
- [`config/risk.yaml`](./config/risk.yaml) — account-wide limits
- [`config/calendar.yaml`](./config/calendar.yaml) — market holidays and sessions

## Timeframes and validation

ORBot supports timeframes from five minutes to two days. Native broker
intervals can be compared directly with exchange-produced bars; resampled
intervals use session-anchored wall-clock buckets and require separate
validation.

Before live execution:

1. Compare warmup and replay output against the source chart.
2. Validate every enabled plan.
3. Run a clean paper-trading period.
4. Confirm broker IDs, static IP requirements, token renewal, and exchange rules.
5. Start with the kill switch available and monitor reconciliation.

## Documentation

- [Project plan](./PLAN.md) — architecture, phases, and validation gate
- [Pine source](./docs/SVMKR_UT_HMA_ORB.pine) — source indicator
- [Validation notes](./docs/VALIDATION.md) — fidelity and operational checks
- [Audit notes](./docs/AUDIT-2026-08-17.md) — security and reliability review
- [Deployment guide](./deploy/README.md) — service installation and hardening

## Project layout

```text
src/orbot/
  broker/       broker clients, feeds, authentication, and instruments
  engine/       candles, timeframes, indicators, and strategy
  execution/    resolver, guards, OMS, and positions
  ingest/       optional webhook and replay sources
  ops/          journal, notifications, calendar, and kill switch
  ui/           lightweight performance dashboard
config/         trade plans, risk limits, and market calendar
tests/          unit, adversarial, broker, engine, and UI tests
```

## Testing

The suite includes adversarial tests for malformed feed frames, stale or
non-finite prices, instrument resolution, resampling boundaries, duplicate
orders, token-cache failures, position reconciliation, and broker-specific
edge cases.

```bash
pytest -q
pytest tests/test_adversarial.py -q
ruff check .
```

## Regulatory and safety notice

Broker API trading may require exchange registration, an approved strategy ID,
a whitelisted static IP, OAuth, and two-factor authentication. Requirements
change by broker and jurisdiction. Confirm your setup with the broker and an
appropriately qualified professional before enabling live orders.

## Contributing

Focused pull requests and bug reports are welcome. Please include tests for
behavior changes, never include credentials or runtime data, and preserve the
fail-closed behavior of the guard layer.

## License

No license has been declared yet. Add a `LICENSE` file before accepting external
contributions or redistributing the project.
