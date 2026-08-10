"""
Handles the Google OAuth2 flow for connecting a user's Drive account.

Flow:
1. bot sends the user a login link built by build_auth_url(state=telegram_user_id)
2. user logs in with Google in their browser and is redirected to OAUTH_REDIRECT_URI
3. a tiny FastAPI route in main.py receives the ?code=...&state=... and calls
   exchange_code(code) to get credentials, then saves them via database.set_google_token
"""
import json
import os
from datetime import datetime

# Google always returns the scopes it actually granted, which can be
# reordered or padded with an implicit "openid"/"...userinfo.email" scope
# even when you didn't ask for it that way. oauthlib treats that as the
# scope having "changed" and raises on flow.fetch_token(), which surfaces to
# the user as the Google consent screen appearing to work and then the bot
# reporting "Login failed" right after. This is the documented workaround
# (must be set before Flow/fetch_token run) - see
# https://github.com/googleapis/google-auth-library-python-oauthlib/issues/45
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleRequest
from googleapiclient.discovery import build

from config import cfg

# Google's OAuth library refuses to run the flow over plain HTTP. That's
# correct for production (Koyeb/Render/etc. are always https), but the
# README's documented local-dev path (OAUTH_REDIRECT_URI=http://localhost:...)
# would otherwise fail with "(insecure_transport) OAuth 2 MUST utilize
# https." on the callback. Only relax this for an explicit localhost/127.0.0.1
# redirect - never for a real deployed domain.
if cfg.OAUTH_REDIRECT_URI.startswith("http://") and (
    "localhost" in cfg.OAUTH_REDIRECT_URI or "127.0.0.1" in cfg.OAUTH_REDIRECT_URI
):
    os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")


def _client_config():
    return {
        "web": {
            "client_id": cfg.GOOGLE_CLIENT_ID,
            "client_secret": cfg.GOOGLE_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [cfg.OAUTH_REDIRECT_URI],
        }
    }


def build_auth_url(state: str) -> str:
    flow = Flow.from_client_config(
        _client_config(), scopes=cfg.GOOGLE_SCOPES, redirect_uri=cfg.OAUTH_REDIRECT_URI
    )
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        include_granted_scopes="true",
        state=state,
    )
    return auth_url


def exchange_code(code: str) -> dict:
    """Exchange an auth code for credentials, return as a JSON-serializable dict."""
    flow = Flow.from_client_config(
        _client_config(), scopes=cfg.GOOGLE_SCOPES, redirect_uri=cfg.OAUTH_REDIRECT_URI
    )
    flow.fetch_token(code=code)
    creds = flow.credentials
    return credentials_to_dict(creds)


def credentials_to_dict(creds: Credentials) -> dict:
    # FIX #4: persist expiry so credentials_from_dict can determine whether
    # the token has expired without having to make a live API call first.
    return {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": creds.scopes,
        "expiry": creds.expiry.isoformat() if creds.expiry else None,
    }


def credentials_from_dict(data: dict) -> Credentials:
    # FIX #4: restore expiry from the persisted ISO string so that
    # creds.expired can be evaluated without a live API round-trip.
    expiry = None
    if data.get("expiry"):
        try:
            expiry = datetime.fromisoformat(data["expiry"])
            # Normalize persisted expiry values for google-auth comparisons.
            if expiry.tzinfo is not None:
                expiry = expiry.replace(tzinfo=None)
        except (ValueError, TypeError):
            expiry = None

    creds = Credentials(
        token=data.get("token"),
        refresh_token=data.get("refresh_token"),
        token_uri=data.get("token_uri"),
        client_id=data.get("client_id"),
        client_secret=data.get("client_secret"),
        scopes=data.get("scopes"),
        expiry=expiry,
    )
    # FIX #4: use 'not creds.valid' instead of just 'creds.expired'.
    # creds.expired is False when expiry is None (even if the token is stale);
    # creds.valid is False when the token is None, expired, or otherwise
    # unusable — so it covers all cases correctly.
    if not creds.valid and creds.refresh_token:
        creds.refresh(GoogleRequest())
        # Keep the in-memory token current for subsequent API calls and for
        # callers that persist the token after a refresh.
        data.update(credentials_to_dict(creds))
    return creds


def get_user_email(creds: Credentials) -> str:
    oauth2 = build("oauth2", "v2", credentials=creds)
    info = oauth2.userinfo().get().execute()
    return info.get("email", "unknown")
