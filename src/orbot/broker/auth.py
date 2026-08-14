"""Access token lifecycle.

Dhan access tokens are valid for **24 hours**. A bot reading a static token from
`.env` therefore stops working every morning — which is exactly the failure an
unattended system cannot have, and it fails at the worst moment: the token dies
overnight, so the first thing that breaks is the market open.

Three ways to hold a token, in descending order of how well they survive being
left alone:

1. **TOTP** — `POST auth.dhan.co/app/generateAccessToken` with the client id, a
   6-digit PIN and a TOTP code. Fully unattended: the bot mints its own token
   whenever the current one is close to expiring. Requires TOTP to be set up on
   the Dhan API page ("Add TOTP").
2. **Renewal** — `GET /v2/RenewToken` extends an existing token by another 24
   hours. Only works for tokens generated from Dhan Web, so it buys a day at a
   time and still needs a human eventually.
3. **Static token** — whatever is in `.env`. Works, but dies within 24 hours.
   Fine for a one-off `orbot signals` run, not for `orbot run`.

The token is cached to disk with its expiry so a restart does not mint a new one
needlessly, and refresh happens *before* expiry rather than after a 401 — an
expired token mid-session means failed order calls at the worst possible time.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import structlog

from ..constants import API_BASE, IST

log = structlog.get_logger(__name__)
IST_TZ = ZoneInfo(IST)

AUTH_BASE = "https://auth.dhan.co"
GENERATE_URL = f"{AUTH_BASE}/app/generateAccessToken"
RENEW_URL = f"{API_BASE}/RenewToken"

# Refresh this long before expiry. Generous on purpose: a token that expires
# mid-session fails order calls, and orders are the one thing that must not fail.
REFRESH_MARGIN = dt.timedelta(hours=2)


class AuthError(RuntimeError):
    """Could not obtain a usable token."""


@dataclass(slots=True)
class TokenInfo:
    token: str
    expires_at: dt.datetime | None = None
    source: str = "static"

    @property
    def is_expired(self) -> bool:
        if self.expires_at is None:
            return False          # unknown expiry — assume usable, warn elsewhere
        return dt.datetime.now(IST_TZ) >= self.expires_at

    @property
    def needs_refresh(self) -> bool:
        if self.expires_at is None:
            return False
        return dt.datetime.now(IST_TZ) >= self.expires_at - REFRESH_MARGIN

    @property
    def hours_left(self) -> float | None:
        if self.expires_at is None:
            return None
        return (self.expires_at - dt.datetime.now(IST_TZ)).total_seconds() / 3600

    def redacted(self) -> str:
        return f"{self.token[:8]}...{self.token[-4:]}" if len(self.token) > 16 else "***"


def _parse_expiry(raw: object) -> dt.datetime | None:
    if not raw:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        log.warning("auth.bad_expiry", value=str(raw)[:60])
        return None
    # Dhan documents expiryTime as IST. A naive value is therefore IST, not UTC —
    # reading it as UTC would put expiry 5h30m late and let a dead token through.
    return parsed.replace(tzinfo=IST_TZ) if parsed.tzinfo is None else parsed


class TokenManager:
    """Provides a currently-valid access token, minting or renewing as needed."""

    def __init__(
        self,
        client_id: str,
        *,
        static_token: str = "",
        pin: str = "",
        totp_secret: str = "",
        cache_path: Path | None = None,
        timeout: float = 15.0,
    ) -> None:
        self.client_id = str(client_id)
        self.pin = pin
        self.totp_secret = totp_secret
        self.cache_path = cache_path
        self._timeout = timeout
        self._info: TokenInfo | None = None

        if static_token:
            self._info = TokenInfo(static_token, None, "static")
        cached = self._load_cache()
        if cached and not cached.is_expired:
            # A cached token that is still good beats a possibly stale .env one.
            self._info = cached

    @property
    def can_self_renew(self) -> bool:
        """True when the bot can mint a token with no human involved."""
        return bool(self.client_id and self.pin and self.totp_secret)

    @property
    def info(self) -> TokenInfo | None:
        return self._info

    # --- public ----------------------------------------------------------
    def token(self) -> str:
        """Return a usable token, refreshing first if it is close to expiring."""
        info = self._info
        if info is not None and not info.needs_refresh:
            return info.token

        if self.can_self_renew:
            try:
                self._info = self.generate()
                return self._info.token
            except AuthError as exc:
                if info is not None and not info.is_expired:
                    log.warning("auth.refresh_failed_using_existing",
                                error=str(exc)[:200],
                                hours_left=round(info.hours_left or 0, 1))
                    return info.token
                raise

        if info is None:
            raise AuthError(
                "no access token available. Either set DHAN_ACCESS_TOKEN, or set "
                "DHAN_PIN and DHAN_TOTP_SECRET so the bot can mint its own."
            )
        if info.is_expired:
            raise AuthError(
                "access token has expired and cannot be renewed automatically. "
                "Set up TOTP on the Dhan API page and configure DHAN_PIN and "
                "DHAN_TOTP_SECRET, or paste a fresh DHAN_ACCESS_TOKEN."
            )
        return info.token

    def generate(self) -> TokenInfo:
        """Mint a fresh 24-hour token using PIN + TOTP."""
        if not self.can_self_renew:
            raise AuthError("TOTP generation needs client id, PIN and TOTP secret")
        code = self._totp_code()
        try:
            resp = httpx.post(GENERATE_URL, timeout=self._timeout, params={
                "dhanClientId": self.client_id, "pin": self.pin, "totp": code,
            })
        except httpx.RequestError as exc:
            raise AuthError(f"token generation request failed: {exc}") from exc

        body = _json(resp)
        if resp.status_code >= 400 or not isinstance(body, dict) or \
                not body.get("accessToken"):
            raise AuthError(
                f"token generation rejected ({resp.status_code}): "
                f"{_message(body)}. Check the PIN, and that TOTP is enabled on "
                "the Dhan API page."
            )

        info = TokenInfo(str(body["accessToken"]),
                         _parse_expiry(body.get("expiryTime")), "totp")
        self._save_cache(info)
        log.info("auth.token_generated", source="totp",
                 expires_at=info.expires_at.isoformat() if info.expires_at else None,
                 hours_left=round(info.hours_left or 0, 1))
        return info

    def renew(self) -> TokenInfo:
        """Extend the current token by another 24 hours.

        Only works for web-generated tokens, so it is a fallback rather than the
        primary path — it still needs a human once the chain is broken.
        """
        if self._info is None:
            raise AuthError("nothing to renew")
        try:
            resp = httpx.get(RENEW_URL, timeout=self._timeout, headers={
                "access-token": self._info.token, "dhanClientId": self.client_id,
            })
        except httpx.RequestError as exc:
            raise AuthError(f"token renewal request failed: {exc}") from exc

        body = _json(resp)
        if resp.status_code >= 400 or not isinstance(body, dict) or \
                not body.get("accessToken"):
            raise AuthError(f"token renewal rejected ({resp.status_code}): "
                            f"{_message(body)}")
        info = TokenInfo(str(body["accessToken"]),
                         _parse_expiry(body.get("expiryTime")), "renewed")
        self._info = info
        self._save_cache(info)
        log.info("auth.token_renewed",
                 expires_at=info.expires_at.isoformat() if info.expires_at else None)
        return info

    def describe(self) -> str:
        info = self._info
        if info is None:
            return "no token"
        if info.expires_at is None:
            return (f"token via {info.source}, expiry unknown — Dhan tokens last "
                    "24h, so this will stop working within a day")
        return (f"token via {info.source}, {info.hours_left:.1f}h left "
                f"(expires {info.expires_at:%Y-%m-%d %H:%M} IST)")

    # --- TOTP ------------------------------------------------------------
    def _totp_code(self) -> str:
        try:
            import pyotp
        except ImportError as exc:
            raise AuthError(
                "pyotp is required for TOTP token generation — pip install pyotp"
            ) from exc
        secret = self.totp_secret.replace(" ", "").upper()
        try:
            return pyotp.TOTP(secret).now()
        except Exception as exc:
            raise AuthError(
                f"could not compute a TOTP code: {exc}. DHAN_TOTP_SECRET should "
                "be the base32 secret shown alongside the QR code, not the "
                "6-digit code itself."
            ) from exc

    # --- cache -----------------------------------------------------------
    def _load_cache(self) -> TokenInfo | None:
        if self.cache_path is None or not self.cache_path.exists():
            return None
        try:
            raw = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        # Valid JSON is not necessarily the shape we wrote. A truncated or
        # hand-edited cache must not crash the process at startup, before the
        # bot has had a chance to do anything.
        if not isinstance(raw, dict):
            log.warning("auth.cache_malformed", path=str(self.cache_path))
            return None
        if raw.get("client_id") != self.client_id or not raw.get("token"):
            return None
        return TokenInfo(raw["token"], _parse_expiry(raw.get("expires_at")),
                         raw.get("source", "cache"))

    def _save_cache(self, info: TokenInfo) -> None:
        if self.cache_path is None:
            return
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps({
                "client_id": self.client_id,
                "token": info.token,
                "expires_at": info.expires_at.isoformat() if info.expires_at else None,
                "source": info.source,
            }), encoding="utf-8")
            # The cache holds a live credential; keep it off other users' hands
            # where the platform supports it.
            try:
                self.cache_path.chmod(0o600)
            except (OSError, NotImplementedError):
                pass
        except OSError as exc:
            log.warning("auth.cache_write_failed", error=str(exc))


def _json(resp: httpx.Response) -> object:
    try:
        return resp.json()
    except ValueError:
        return resp.text


def _message(body: object) -> str:
    if isinstance(body, dict):
        return str(body.get("errorMessage") or body.get("message") or body)
    return str(body)[:200]


__all__ = ["TokenManager", "TokenInfo", "AuthError", "REFRESH_MARGIN"]
