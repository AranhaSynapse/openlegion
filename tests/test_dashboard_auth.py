"""Tests for dashboard session cookie verification (src/dashboard/auth.py)."""

from __future__ import annotations

import hashlib
import hmac
import time
from unittest.mock import patch

import pytest

from src.dashboard.auth import (
    COOKIE_MAX_AGE,
    reset_cache,
    verify_session_cookie,
)


def _make_cookie(token: str, max_age: int = COOKIE_MAX_AGE) -> str:
    """Create a valid ol_session cookie value for testing."""
    key = hmac.new(token.encode(), b"ol-cookie-signing", hashlib.sha256).digest()
    expiry = str(int(time.time()) + max_age)
    sig = hmac.new(key, expiry.encode(), hashlib.sha256).hexdigest()
    return f"{expiry}.{sig}"


@pytest.fixture(autouse=True)
def _reset_auth_cache():
    """Reset the cached cookie key before each test."""
    reset_cache()
    yield
    reset_cache()


class TestVerifySessionCookie:
    def test_dev_mode_no_access_token(self, tmp_path):
        """No access token file => dev mode, all cookies pass."""
        with patch("src.dashboard.auth._ACCESS_TOKEN_PATH", str(tmp_path / "nonexistent")):
            assert verify_session_cookie("") is None
            assert verify_session_cookie("anything") is None

    def test_valid_cookie(self, tmp_path):
        """Correctly signed, not expired cookie passes."""
        token_file = tmp_path / "access_token"
        token_file.write_text("test-secret-token")
        with patch("src.dashboard.auth._ACCESS_TOKEN_PATH", str(token_file)):
            cookie = _make_cookie("test-secret-token")
            assert verify_session_cookie(cookie) is None

    def test_expired_cookie(self, tmp_path):
        """Valid signature but past expiry is rejected."""
        token_file = tmp_path / "access_token"
        token_file.write_text("test-secret-token")
        with patch("src.dashboard.auth._ACCESS_TOKEN_PATH", str(token_file)):
            cookie = _make_cookie("test-secret-token", max_age=-100)
            result = verify_session_cookie(cookie)
            assert result is not None
            assert "expired" in result.lower()

    def test_invalid_signature(self, tmp_path):
        """Wrong HMAC signature is rejected."""
        token_file = tmp_path / "access_token"
        token_file.write_text("correct-token")
        with patch("src.dashboard.auth._ACCESS_TOKEN_PATH", str(token_file)):
            # Cookie signed with wrong token
            cookie = _make_cookie("wrong-token")
            result = verify_session_cookie(cookie)
            assert result is not None
            assert "invalid" in result.lower()

    def test_malformed_cookie_no_dot(self, tmp_path):
        """Cookie without dot separator is rejected."""
        token_file = tmp_path / "access_token"
        token_file.write_text("test-token")
        with patch("src.dashboard.auth._ACCESS_TOKEN_PATH", str(token_file)):
            result = verify_session_cookie("nodothere")
            assert result is not None

    def test_malformed_cookie_non_integer_expiry(self, tmp_path):
        """Cookie with non-integer expiry is rejected."""
        token_file = tmp_path / "access_token"
        token_file.write_text("test-token")
        with patch("src.dashboard.auth._ACCESS_TOKEN_PATH", str(token_file)):
            result = verify_session_cookie("notanumber.abcdef")
            assert result is not None
            assert "invalid" in result.lower()

    def test_empty_cookie_in_hosted_mode(self, tmp_path):
        """Empty cookie string in hosted mode is rejected."""
        token_file = tmp_path / "access_token"
        token_file.write_text("test-token")
        with patch("src.dashboard.auth._ACCESS_TOKEN_PATH", str(token_file)):
            result = verify_session_cookie("")
            assert result is not None
            assert "required" in result.lower()

    def test_future_expiry_too_far(self, tmp_path):
        """Cookie with expiry unreasonably far in the future is rejected."""
        token_file = tmp_path / "access_token"
        token_file.write_text("test-token")
        with patch("src.dashboard.auth._ACCESS_TOKEN_PATH", str(token_file)):
            # Cookie valid for 1 year (way past 24h max + 5min tolerance)
            cookie = _make_cookie("test-token", max_age=365 * 24 * 3600)
            result = verify_session_cookie(cookie)
            assert result is not None
            assert "invalid" in result.lower()

    def test_reset_cache_reloads(self, tmp_path):
        """After reset_cache(), the key is re-read from disk."""
        token_file = tmp_path / "access_token"
        # Start with no token
        with patch("src.dashboard.auth._ACCESS_TOKEN_PATH", str(tmp_path / "nonexistent")):
            assert verify_session_cookie("") is None  # dev mode

        reset_cache()

        # Now create the token file
        token_file.write_text("new-token")
        with patch("src.dashboard.auth._ACCESS_TOKEN_PATH", str(token_file)):
            # Empty cookie should now be rejected (hosted mode)
            result = verify_session_cookie("")
            assert result is not None
            assert "required" in result.lower()

    def test_empty_access_token_file(self, tmp_path):
        """Empty access token file => dev mode."""
        token_file = tmp_path / "access_token"
        token_file.write_text("")
        with patch("src.dashboard.auth._ACCESS_TOKEN_PATH", str(token_file)):
            assert verify_session_cookie("") is None
