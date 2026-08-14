"""Access token lifecycle tests.

Dhan tokens live 24 hours. A bot holding a static token stops working every
morning, and it fails at the worst moment — the token dies overnight, so the
first thing to break is the market open, unattended, with positions possibly
still on.

The properties that matter: refresh happens *before* expiry rather than after a
failure, a refresh outage does not throw away a token that is still good, and
the expiry timestamp is read as IST rather than UTC.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

from orbot.broker.auth import (
    REFRESH_MARGIN,
    AuthError,
    TokenInfo,
    TokenManager,
    _parse_expiry,
)

IST = ZoneInfo("Asia/Kolkata")
# A valid base32 secret, the format Dhan shows next to the QR code.
TOTP_SECRET = "JBSWY3DPEHPK3PXP"


def in_hours(hours: float) -> dt.datetime:
    return dt.datetime.now(IST) + dt.timedelta(hours=hours)


class TestTokenInfo:
    def test_a_future_expiry_is_not_expired(self):
        assert not TokenInfo("t", in_hours(5)).is_expired

    def test_a_past_expiry_is_expired(self):
        assert TokenInfo("t", in_hours(-1)).is_expired

    def test_refresh_is_triggered_before_expiry_not_after(self):
        """Waiting for actual expiry means the first call after it fails, and if
        that call is an order placement its outcome becomes ambiguous."""
        margin_hours = REFRESH_MARGIN.total_seconds() / 3600
        assert TokenInfo("t", in_hours(margin_hours - 0.5)).needs_refresh
        assert not TokenInfo("t", in_hours(margin_hours + 1)).needs_refresh

    def test_an_unknown_expiry_never_reports_expired(self):
        """A static token from .env has no expiry field. Treating unknown as
        expired would break one-off commands that work fine."""
        info = TokenInfo("t", None)
        assert not info.is_expired and not info.needs_refresh
        assert info.hours_left is None

    def test_redaction_hides_the_middle(self):
        info = TokenInfo("eyJhbGciOiJIUzI1NiJ9.payload.signature")
        assert "payload" not in info.redacted()

    def test_a_short_token_is_fully_redacted(self):
        assert TokenInfo("short").redacted() == "***"


class TestExpiryParsing:
    def test_a_naive_timestamp_is_read_as_ist(self):
        """Dhan documents expiryTime as IST. Reading a naive value as UTC would
        put expiry 5h30m late and let a dead token through."""
        parsed = _parse_expiry("2026-08-14T23:30:00")
        assert parsed.tzinfo is not None
        assert parsed.utcoffset().total_seconds() == 5.5 * 3600

    def test_an_explicit_offset_is_respected(self):
        parsed = _parse_expiry("2026-08-14T18:00:00+00:00")
        assert parsed.utcoffset().total_seconds() == 0

    def test_a_z_suffix_is_handled(self):
        assert _parse_expiry("2026-08-14T18:00:00Z") is not None

    @pytest.mark.parametrize("bad", ["", None, "not-a-date", "garbage"])
    def test_garbage_becomes_none(self, bad):
        assert _parse_expiry(bad) is None


class TestCapability:
    def test_pin_and_totp_enable_self_renewal(self):
        manager = TokenManager("100", pin="1234", totp_secret=TOTP_SECRET)
        assert manager.can_self_renew

    @pytest.mark.parametrize("kwargs", [
        {"pin": "1234"},                      # no secret
        {"totp_secret": TOTP_SECRET},         # no pin
        {},                                   # neither
    ])
    def test_partial_credentials_do_not(self, kwargs):
        assert not TokenManager("100", **kwargs).can_self_renew

    def test_a_static_token_alone_cannot_self_renew(self):
        manager = TokenManager("100", static_token="abc")
        assert not manager.can_self_renew
        assert manager.token() == "abc"

    def test_no_token_at_all_raises_with_a_useful_message(self):
        with pytest.raises(AuthError, match="DHAN_PIN"):
            TokenManager("100").token()

    def test_an_expired_static_token_raises_rather_than_being_used(self):
        manager = TokenManager("100")
        manager._info = TokenInfo("stale", in_hours(-1), "static")
        with pytest.raises(AuthError, match="expired"):
            manager.token()


class TestGeneration:
    def test_a_successful_generation_stores_token_and_expiry(self, monkeypatch, tmp_path):
        expiry = in_hours(24)

        def fake_post(url, **kwargs):
            assert "generateAccessToken" in url
            params = kwargs["params"]
            assert params["dhanClientId"] == "100"
            assert params["pin"] == "1234"
            assert len(params["totp"]) == 6 and params["totp"].isdigit()
            return httpx.Response(200, json={
                "accessToken": "fresh-jwt", "expiryTime": expiry.isoformat()})

        monkeypatch.setattr(httpx, "post", fake_post)
        manager = TokenManager("100", pin="1234", totp_secret=TOTP_SECRET,
                               cache_path=tmp_path / "tok.json")
        info = manager.generate()
        assert info.token == "fresh-jwt"
        assert info.source == "totp"
        assert 23 < (info.hours_left or 0) <= 24

    def test_a_rejected_generation_raises_with_guidance(self, monkeypatch):
        monkeypatch.setattr(httpx, "post", lambda url, **kw: httpx.Response(
            401, json={"errorMessage": "invalid pin"}))
        manager = TokenManager("100", pin="0000", totp_secret=TOTP_SECRET)
        with pytest.raises(AuthError, match="TOTP is enabled"):
            manager.generate()

    def test_a_response_without_a_token_is_rejected(self, monkeypatch):
        monkeypatch.setattr(httpx, "post", lambda url, **kw: httpx.Response(
            200, json={"status": "ok"}))
        manager = TokenManager("100", pin="1234", totp_secret=TOTP_SECRET)
        with pytest.raises(AuthError):
            manager.generate()

    def test_a_network_failure_raises_auth_error(self, monkeypatch):
        def boom(url, **kwargs):
            raise httpx.ConnectError("no route to host")
        monkeypatch.setattr(httpx, "post", boom)
        manager = TokenManager("100", pin="1234", totp_secret=TOTP_SECRET)
        with pytest.raises(AuthError, match="request failed"):
            manager.generate()

    def test_a_bad_totp_secret_says_what_to_paste(self, monkeypatch):
        manager = TokenManager("100", pin="1234", totp_secret="123456")
        with pytest.raises(AuthError, match="base32"):
            manager.generate()


class TestRefreshBehaviour:
    def test_a_token_close_to_expiry_is_refreshed_automatically(self, monkeypatch):
        calls = {"n": 0}

        def fake_post(url, **kwargs):
            calls["n"] += 1
            return httpx.Response(200, json={
                "accessToken": f"jwt-{calls['n']}",
                "expiryTime": in_hours(24).isoformat()})

        monkeypatch.setattr(httpx, "post", fake_post)
        manager = TokenManager("100", pin="1234", totp_secret=TOTP_SECRET)
        manager._info = TokenInfo("about-to-die", in_hours(0.5), "totp")
        assert manager.token() == "jwt-1"
        assert calls["n"] == 1

    def test_a_healthy_token_is_not_refreshed(self, monkeypatch):
        def boom(url, **kwargs):
            raise AssertionError("should not have refreshed")
        monkeypatch.setattr(httpx, "post", boom)
        manager = TokenManager("100", pin="1234", totp_secret=TOTP_SECRET)
        manager._info = TokenInfo("healthy", in_hours(20), "totp")
        assert manager.token() == "healthy"

    def test_a_failed_refresh_keeps_using_a_still_valid_token(self, monkeypatch):
        """A transient auth outage must not take the bot down while it still
        holds a usable token — that would turn a blip into a halt mid-session."""
        def boom(url, **kwargs):
            raise httpx.ConnectError("auth server down")
        monkeypatch.setattr(httpx, "post", boom)
        manager = TokenManager("100", pin="1234", totp_secret=TOTP_SECRET)
        manager._info = TokenInfo("still-good", in_hours(1), "totp")
        assert manager.token() == "still-good"

    def test_a_failed_refresh_on_an_expired_token_raises(self, monkeypatch):
        def boom(url, **kwargs):
            raise httpx.ConnectError("auth server down")
        monkeypatch.setattr(httpx, "post", boom)
        manager = TokenManager("100", pin="1234", totp_secret=TOTP_SECRET)
        manager._info = TokenInfo("dead", in_hours(-1), "totp")
        with pytest.raises(AuthError):
            manager.token()


class TestRenewal:
    def test_renewal_swaps_in_the_new_token(self, monkeypatch):
        monkeypatch.setattr(httpx, "get", lambda url, **kw: httpx.Response(
            200, json={"accessToken": "renewed-jwt",
                       "expiryTime": in_hours(24).isoformat()}))
        manager = TokenManager("100", static_token="old")
        info = manager.renew()
        assert info.token == "renewed-jwt" and info.source == "renewed"
        assert manager.token() == "renewed-jwt"

    def test_renewal_with_no_token_raises(self):
        with pytest.raises(AuthError, match="nothing to renew"):
            TokenManager("100").renew()

    def test_a_rejected_renewal_raises(self, monkeypatch):
        monkeypatch.setattr(httpx, "get", lambda url, **kw: httpx.Response(
            401, json={"errorMessage": "token not web-generated"}))
        with pytest.raises(AuthError):
            TokenManager("100", static_token="old").renew()


class TestCache:
    def test_a_generated_token_is_cached(self, monkeypatch, tmp_path):
        cache = tmp_path / "tok.json"
        monkeypatch.setattr(httpx, "post", lambda url, **kw: httpx.Response(
            200, json={"accessToken": "cached-jwt",
                       "expiryTime": in_hours(24).isoformat()}))
        TokenManager("100", pin="1234", totp_secret=TOTP_SECRET,
                     cache_path=cache).generate()
        assert json.loads(cache.read_text())["token"] == "cached-jwt"

    def test_a_valid_cached_token_is_reused_on_startup(self, tmp_path):
        """A restart must not mint a new token needlessly — Dhan expires the old
        one when a new is issued, which would break any other session."""
        cache = tmp_path / "tok.json"
        cache.write_text(json.dumps({
            "client_id": "100", "token": "from-cache",
            "expires_at": in_hours(20).isoformat(), "source": "totp"}))
        manager = TokenManager("100", pin="1234", totp_secret=TOTP_SECRET,
                               cache_path=cache)
        assert manager.token() == "from-cache"

    def test_an_expired_cached_token_is_ignored(self, tmp_path):
        cache = tmp_path / "tok.json"
        cache.write_text(json.dumps({
            "client_id": "100", "token": "stale",
            "expires_at": in_hours(-1).isoformat(), "source": "totp"}))
        manager = TokenManager("100", static_token="from-env", cache_path=cache)
        assert manager.token() == "from-env"

    def test_a_cache_for_a_different_client_is_ignored(self, tmp_path):
        """Guards against picking up another account's token after a config
        change — that would place orders on the wrong account."""
        cache = tmp_path / "tok.json"
        cache.write_text(json.dumps({
            "client_id": "999", "token": "other-account",
            "expires_at": in_hours(20).isoformat()}))
        manager = TokenManager("100", static_token="mine", cache_path=cache)
        assert manager.token() == "mine"

    @pytest.mark.parametrize("content", ["", "{not json", "[]"])
    def test_a_corrupt_cache_is_ignored_not_fatal(self, tmp_path, content):
        cache = tmp_path / "tok.json"
        cache.write_text(content)
        assert TokenManager("100", static_token="env", cache_path=cache).token() == "env"


class TestClientIntegration:
    def test_the_client_accepts_a_manager_and_sends_a_live_token(self, monkeypatch):
        from orbot.broker.dhan_client import DhanClient

        monkeypatch.setattr(httpx, "post", lambda url, **kw: httpx.Response(
            200, json={"accessToken": "minted",
                       "expiryTime": in_hours(24).isoformat()}))
        manager = TokenManager("100", pin="1234", totp_secret=TOTP_SECRET)
        client = DhanClient("100", manager)
        try:
            client._ensure_token()
            assert client._data.headers["access-token"] == "minted"
        finally:
            client.close()

    def test_a_plain_string_token_still_works(self):
        from orbot.broker.dhan_client import DhanClient
        client = DhanClient("100", "static-token")
        try:
            client._ensure_token()
            assert client._data.headers["access-token"] == "static-token"
        finally:
            client.close()

    def test_the_feed_url_is_rebuilt_from_the_provider_each_time(self):
        """A socket that drops overnight must reconnect with a current token."""
        from orbot.broker.dhan_feed import DhanFeed
        tokens = iter(["first", "second"])
        feed = DhanFeed("100", lambda: next(tokens))
        assert "token=first" in feed.url
        assert "token=second" in feed.url

    def test_the_token_is_kept_out_of_logs(self):
        from orbot.logging_setup import _redact
        event = _redact(None, "info", {"url": "wss://x?version=2&token=SECRET123&c=1"})
        assert "SECRET123" not in event["url"]
