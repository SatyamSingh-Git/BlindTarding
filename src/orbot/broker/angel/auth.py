"""Angel One SmartAPI authentication.

Login returns three tokens with different jobs, and mixing them up produces
confusing 401s rather than a clear error:

* **jwtToken** — bearer token for every REST call.
* **refreshToken** — exchanges for a fresh jwt without re-entering a TOTP.
* **feedToken** — a *separate* credential the WebSocket needs. The REST jwt
  will not authenticate the feed.

Login is `clientcode` + MPIN + TOTP, so unattended operation needs the TOTP
secret in config — same arrangement as the Dhan adapter, and for the same
reason: a bot that needs a human every morning is not unattended.

Angel requires client IP and MAC headers on every request. They are not verified
in any meaningful way, but the API rejects requests that omit them.
"""

from __future__ import annotations

import datetime as dt
import json
import socket
import uuid
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import structlog

from ...constants import IST

log = structlog.get_logger(__name__)
IST_TZ = ZoneInfo(IST)

API_BASE = "https://apiconnect.angelone.in"
LOGIN_PATH = "/rest/auth/angelbroking/user/v1/loginByPassword"
REFRESH_PATH = "/rest/auth/angelbroking/jwt/v1/generateTokens"
PROFILE_PATH = "/rest/secure/angelbroking/user/v1/getProfile"

# Angel's jwt is documented as lasting the trading day. Refresh well before the
# stated expiry when we know it, and otherwise refresh on a fixed schedule
# rather than waiting for a 401 mid-session.
REFRESH_MARGIN = dt.timedelta(hours=2)
ASSUMED_LIFETIME = dt.timedelta(hours=12)


class AngelAuthError(RuntimeError):
    """Could not obtain usable Angel credentials."""


@dataclass(slots=True)
class AngelSession:
    jwt_token: str
    refresh_token: str = ""
    feed_token: str = ""
    expires_at: dt.datetime | None = None
    client_code: str = ""

    @property
    def is_expired(self) -> bool:
        return self.expires_at is not None and dt.datetime.now(IST_TZ) >= self.expires_at

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
        t = self.jwt_token
        return f"{t[:8]}...{t[-4:]}" if len(t) > 16 else "***"


def _local_ip() -> str:
    try:
        return socket.gethostbyname(socket.gethostname())
    except OSError:
        return "127.0.0.1"


def _mac() -> str:
    node = uuid.getnode()
    return ":".join(f"{(node >> shift) & 0xFF:02X}" for shift in range(40, -8, -8))


class AngelAuth:
    """Holds a live SmartAPI session, refreshing before it lapses."""

    def __init__(
        self,
        client_code: str,
        api_key: str,
        *,
        mpin: str = "",
        totp_secret: str = "",
        cache_path: Path | None = None,
        timeout: float = 15.0,
    ) -> None:
        self.client_code = str(client_code)
        self.api_key = api_key
        self.mpin = mpin
        self.totp_secret = totp_secret
        self.cache_path = cache_path
        self._timeout = timeout
        self._session: AngelSession | None = self._load_cache()

    @property
    def can_self_renew(self) -> bool:
        return bool(self.client_code and self.api_key and self.mpin and self.totp_secret)

    @property
    def session(self) -> AngelSession | None:
        return self._session

    # --- headers ---------------------------------------------------------
    def base_headers(self) -> dict[str, str]:
        """Angel rejects requests missing the IP/MAC headers, so they are sent
        on every call even though they are not meaningfully verified."""
        return {
            "Content-type": "application/json",
            "Accept": "application/json",
            "X-UserType": "USER",
            "X-SourceID": "WEB",
            "X-ClientLocalIP": _local_ip(),
            "X-ClientPublicIP": _local_ip(),
            "X-MACAddress": _mac(),
            "X-PrivateKey": self.api_key,
        }

    def auth_headers(self) -> dict[str, str]:
        return {**self.base_headers(),
                "Authorization": f"Bearer {self.jwt()}"}

    # --- session ---------------------------------------------------------
    def jwt(self) -> str:
        session = self._session
        if session is not None and not session.needs_refresh:
            return session.jwt_token

        if session is not None and session.refresh_token:
            try:
                return self.refresh().jwt_token
            except AngelAuthError as exc:
                if not session.is_expired:
                    log.warning("angel.refresh_failed_using_existing",
                                error=str(exc)[:200])
                    return session.jwt_token
                log.warning("angel.refresh_failed_relogging_in", error=str(exc)[:200])

        if self.can_self_renew:
            return self.login().jwt_token

        if session is None:
            raise AngelAuthError(
                "no Angel session. Set ANGEL_CLIENT_CODE, ANGEL_API_KEY, "
                "ANGEL_MPIN and ANGEL_TOTP_SECRET so the bot can log itself in."
            )
        if session.is_expired:
            raise AngelAuthError(
                "Angel session expired and cannot be renewed automatically — "
                "configure ANGEL_MPIN and ANGEL_TOTP_SECRET."
            )
        return session.jwt_token

    def feed_token(self) -> str:
        """The WebSocket needs this, not the REST jwt."""
        self.jwt()          # ensures a live session
        if self._session is None or not self._session.feed_token:
            raise AngelAuthError(
                "no feed token — the market feed cannot authenticate with the "
                "REST jwt; a fresh login is required"
            )
        return self._session.feed_token

    def login(self) -> AngelSession:
        if not self.can_self_renew:
            raise AngelAuthError("login needs client code, API key, MPIN and TOTP secret")
        payload = {
            "clientcode": self.client_code,
            "password": self.mpin,
            "totp": self._totp_code(),
            "state": "orbot",
        }
        body = self._post(LOGIN_PATH, payload, self.base_headers())
        data = _data(body)
        if not data.get("jwtToken"):
            raise AngelAuthError(
                f"login rejected: {_message(body)}. Check the MPIN, and that the "
                "TOTP secret matches the authenticator you registered."
            )
        session = AngelSession(
            jwt_token=str(data["jwtToken"]).removeprefix("Bearer "),
            refresh_token=str(data.get("refreshToken", "")),
            feed_token=str(data.get("feedToken", "")),
            expires_at=dt.datetime.now(IST_TZ) + ASSUMED_LIFETIME,
            client_code=self.client_code,
        )
        self._session = session
        self._save_cache(session)
        log.info("angel.logged_in", client_code=self.client_code,
                 has_feed_token=bool(session.feed_token))
        return session

    def refresh(self) -> AngelSession:
        session = self._session
        if session is None or not session.refresh_token:
            raise AngelAuthError("no refresh token available")
        headers = {**self.base_headers(),
                   "Authorization": f"Bearer {session.jwt_token}"}
        body = self._post(REFRESH_PATH, {"refreshToken": session.refresh_token},
                          headers)
        data = _data(body)
        if not data.get("jwtToken"):
            raise AngelAuthError(f"token refresh rejected: {_message(body)}")

        renewed = AngelSession(
            jwt_token=str(data["jwtToken"]).removeprefix("Bearer "),
            refresh_token=str(data.get("refreshToken") or session.refresh_token),
            # A refresh response may omit feedToken; losing it would silently
            # break the market feed on the next reconnect.
            feed_token=str(data.get("feedToken") or session.feed_token),
            expires_at=dt.datetime.now(IST_TZ) + ASSUMED_LIFETIME,
            client_code=self.client_code,
        )
        self._session = renewed
        self._save_cache(renewed)
        log.info("angel.token_refreshed")
        return renewed

    def describe(self) -> str:
        session = self._session
        if session is None:
            return "no Angel session"
        left = session.hours_left
        return (f"Angel session for {session.client_code}, "
                f"{'unknown expiry' if left is None else f'{left:.1f}h left'}, "
                f"feed token {'present' if session.feed_token else 'MISSING'}")

    # --- transport -------------------------------------------------------
    def _post(self, path: str, payload: dict, headers: dict) -> object:
        try:
            resp = httpx.post(f"{API_BASE}{path}", json=payload, headers=headers,
                              timeout=self._timeout)
        except httpx.RequestError as exc:
            raise AngelAuthError(f"{path} request failed: {exc}") from exc
        try:
            body = resp.json()
        except ValueError:
            raise AngelAuthError(f"{path} returned non-JSON ({resp.status_code})") from None
        if resp.status_code >= 400 or (isinstance(body, dict)
                                       and body.get("status") is False):
            raise AngelAuthError(f"{path} -> {resp.status_code}: {_message(body)}")
        return body

    def _totp_code(self) -> str:
        try:
            import pyotp
        except ImportError as exc:
            raise AngelAuthError("pyotp is required — pip install pyotp") from exc
        try:
            return pyotp.TOTP(self.totp_secret.replace(" ", "").upper()).now()
        except Exception as exc:
            raise AngelAuthError(
                f"could not compute a TOTP code: {exc}. ANGEL_TOTP_SECRET should "
                "be the base32 secret from the QR code, not a 6-digit code."
            ) from exc

    # --- cache -----------------------------------------------------------
    def _load_cache(self) -> AngelSession | None:
        if self.cache_path is None or not self.cache_path.exists():
            return None
        try:
            raw = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(raw, dict) or raw.get("client_code") != self.client_code:
            return None
        expires = raw.get("expires_at")
        session = AngelSession(
            jwt_token=raw.get("jwt_token", ""),
            refresh_token=raw.get("refresh_token", ""),
            feed_token=raw.get("feed_token", ""),
            expires_at=dt.datetime.fromisoformat(expires) if expires else None,
            client_code=self.client_code,
        )
        return session if session.jwt_token and not session.is_expired else None

    def _save_cache(self, session: AngelSession) -> None:
        if self.cache_path is None:
            return
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps({
                "client_code": session.client_code,
                "jwt_token": session.jwt_token,
                "refresh_token": session.refresh_token,
                "feed_token": session.feed_token,
                "expires_at": (session.expires_at.isoformat()
                               if session.expires_at else None),
            }), encoding="utf-8")
            try:
                self.cache_path.chmod(0o600)
            except (OSError, NotImplementedError):
                pass
        except OSError as exc:
            log.warning("angel.cache_write_failed", error=str(exc))


def _data(body: object) -> dict:
    if isinstance(body, dict) and isinstance(body.get("data"), dict):
        return body["data"]
    return {}


def _message(body: object) -> str:
    if isinstance(body, dict):
        return str(body.get("message") or body.get("errorcode") or body)[:300]
    return str(body)[:300]


__all__ = ["AngelAuth", "AngelSession", "AngelAuthError", "API_BASE"]
