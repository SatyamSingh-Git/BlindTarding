# Running ORBot on an Always Free instance

Written for **Ubuntu 24.04** on `VM.Standard.E2.1.Micro` (1 GB) — Ampere was
unavailable in Hyderabad. It fits, but only because the instrument cache cut
the startup peak from 180 MB to 22 MB. If you ever get an A1 shape, none of
the memory notes below matter.

## Why this exists

Run from a laptop shell, the bot died four times in three days and was down
24 of 60 hours — including a whole morning session. `systemd` with
`Restart=always` is the fix. The static public IP is the second reason:
Angel requires a whitelisted IP for order placement and allows one change per
week, which a home broadband address cannot satisfy.

## Install

```bash
ssh -i orbot.key ubuntu@<ip>
git clone https://github.com/SatyamSingh-Git/BlindTarding.git orbot
cd orbot
bash deploy/setup.sh
```

Then copy credentials across. **`.env` must never reach git** — it carries
`ANGEL_MPIN` and `ANGEL_TOTP_SECRET`, which together are full trading access:

```bash
scp -i orbot.key .env ubuntu@<ip>:~/orbot/.env
```

Check before starting:

```bash
./.venv/bin/orbot doctor
./.venv/bin/orbot validate
sudo systemctl start orbot
```

Then harden:

```bash
bash deploy/harden.sh
```

## Watching it

```bash
journalctl -u orbot -f              # live
journalctl -u orbot --since today   # today
systemctl status orbot              # up? how long? restarts?
```

The dashboard deliberately has no public route. It carries your positions and
a kill switch, so it stays on `127.0.0.1` and you tunnel to it:

```bash
ssh -i orbot.key -L 8777:localhost:8777 ubuntu@<ip>
./.venv/bin/orbot dashboard         # inside that session
```

Then open <http://localhost:8777> on your own machine. Running it on demand
also keeps ~230 MB free, which matters on a 1 GB box.

## Stopping it

```bash
sudo systemctl stop orbot     # stops the engine; positions stay open
./.venv/bin/orbot halt        # kill switch: no new orders, flattens
./.venv/bin/orbot flatten     # close everything now
```

`systemctl stop` leaves positions open — the bot reconciles them on restart.
Use `halt` if you want it to stand down while staying up.

## Keeping it alive

- **Contracts expire.** Both plans use `roll: front`, so they follow the front
  month, but the scrip master must be fresh or the roll resolves to a dead
  token. `orbot instruments` daily, ideally on a timer.
- **Oracle reclaims idle Always Free compute.** This bot is near-idle between
  bar closes. Upgrading the tenancy to Pay As You Go keeps Always Free
  resources free but exempts them from reclamation. Worth doing deliberately
  rather than discovering it when the instance disappears.
- **Angel sessions die at midnight.** The client re-logs in via TOTP, headless.
  It has been observed failing once; `Restart=always` covers the rest.

## Before ever switching to live

`.env` still says `TRADE_MODE=paper`. Before that changes:

1. Whitelist this instance's public IP with Angel — orders are rejected
   otherwise, and you get one IP change per week.
2. Re-read `docs/AUDIT-2026-08-17.md`. Thirty-six defects were found in three
   days of running this, several of which produced plausible wrong numbers
   rather than errors.
3. Set `assumed_margin_pct` and the fat-finger caps against your real balance,
   not the paper one.
