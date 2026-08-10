from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional, Any

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from ..config import cfg


@dataclass
class GoogleClientRecord:
    name: str
    client_id: str
    client_secret: str
    refresh_token: str
    enabled: bool = True
    status: str = "AVAILABLE"  # AVAILABLE, RATE_LIMITED, QUOTA_EXCEEDED, ERROR, DISABLED
    request_count: int = 0
    success_count: int = 0
    error_count: int = 0
    quota_errors: int = 0
    last_used: Optional[float] = None
    cooldown_until: Optional[float] = None

    def is_healthy(self) -> bool:
        if not self.enabled:
            return False
        if self.cooldown_until and time.time() < self.cooldown_until:
            return False
        return True


class GoogleClientManager:
    """Manage one or more Google OAuth clients and provide automatic failover.

    Usage:
      from bot.google_client_manager import google_manager
      creds = google_manager.get_credentials_for_next()
      # use creds with googleapiclient.build(..., credentials=creds)
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._clients: Dict[str, GoogleClientRecord] = {}
        self._order: List[str] = []
        self._rr_index = 0
        self.load_from_config()

    def load_from_config(self) -> None:
        """Load clients from `cfg`. Respects single-client fallback."""
        with self._lock:
            self._clients.clear()
            self._order.clear()

            if cfg.GOOGLE_MULTI_CLIENT_ENABLED and cfg.GOOGLE_CLIENTS:
                try:
                    data = json.loads(cfg.GOOGLE_CLIENTS)
                    if isinstance(data, list):
                        for idx, item in enumerate(data):
                            name = item.get("name") or f"client-{idx+1}"
                            record = GoogleClientRecord(
                                name=name,
                                client_id=item.get("client_id", ""),
                                client_secret=item.get("client_secret", ""),
                                refresh_token=item.get("refresh_token", ""),
                                enabled=bool(item.get("enabled", True)),
                            )
                            if record.client_id and record.client_secret and record.refresh_token:
                                self._clients[record.client_id] = record
                                self._order.append(record.client_id)
                except Exception:
                    # Don't raise; keep manager empty so callers can fallback.
                    pass
            # Fallback: single primary client from legacy envs
            if not self._clients and cfg.GOOGLE_CLIENT_ID and cfg.GOOGLE_CLIENT_SECRET:
                record = GoogleClientRecord(
                    name="primary",
                    client_id=cfg.GOOGLE_CLIENT_ID,
                    client_secret=cfg.GOOGLE_CLIENT_SECRET,
                    refresh_token=cfg.GOOGLE_REFRESH_TOKEN or "",
                    enabled=True,
                )
                self._clients[record.client_id] = record
                self._order.append(record.client_id)

    def reload(self) -> None:
        """Reload configuration at runtime."""
        self.load_from_config()

    def list_clients(self) -> List[GoogleClientRecord]:
        with self._lock:
            return [self._clients[k] for k in self._order]

    def _next_index(self) -> int:
        with self._lock:
            if not self._order:
                return -1
            self._rr_index = (self._rr_index + 1) % len(self._order)
            return self._rr_index

    def get_available_client(self) -> Optional[GoogleClientRecord]:
        """Return the next healthy client (round-robin)."""
        with self._lock:
            if not self._order:
                return None
            n = len(self._order)
            for i in range(n):
                idx = (self._rr_index + i) % n
                cid = self._order[idx]
                rec = self._clients.get(cid)
                if rec and rec.is_healthy():
                    # advance rr index to next position for fairness
                    self._rr_index = idx
                    rec.last_used = time.time()
                    rec.request_count += 1
                    return rec
            return None

    def mark_success(self, client_id: str) -> None:
        with self._lock:
            rec = self._clients.get(client_id)
            if rec:
                rec.success_count += 1
                rec.status = "AVAILABLE"

    def mark_error(self, client_id: str, error_type: str = "ERROR") -> None:
        with self._lock:
            rec = self._clients.get(client_id)
            if not rec:
                return
            rec.error_count += 1
            if "quota" in error_type.lower():
                rec.quota_errors += 1
                rec.status = "QUOTA_EXCEEDED"
                # short cooldown
                rec.cooldown_until = time.time() + 60 * 5
            elif "rate" in error_type.lower() or "limit" in error_type.lower():
                rec.status = "RATE_LIMITED"
                rec.cooldown_until = time.time() + 30
            else:
                rec.status = "ERROR"

    def set_enabled(self, client_id: str, enabled: bool) -> None:
        with self._lock:
            rec = self._clients.get(client_id)
            if rec:
                rec.enabled = enabled
                rec.status = "DISABLED" if not enabled else "AVAILABLE"

    def get_credentials_for(self, client: GoogleClientRecord) -> Credentials:
        # Create a Credentials object using only refresh token; token will be
        # obtained on demand by google-auth when used with an http transport.
        return Credentials(
            token=None,
            refresh_token=client.refresh_token,
            client_id=client.client_id,
            client_secret=client.client_secret,
            token_uri="https://oauth2.googleapis.com/token",
        )

    def execute(self, operation: Callable[..., Any], *args, **kwargs) -> Any:
        """Attempt `operation` with available clients; on quota/rate errors try another.

        The `operation` callable should accept a `credentials` kwarg or positional
        arg as first parameter; the manager will pass a `google.oauth2.credentials.Credentials`
        instance.
        """
        tried: List[str] = []
        last_exc: Optional[Exception] = None
        # iterate over a snapshot of clients so dynamic reloads don't confuse loop
        clients_snapshot = self.list_clients()
        if not clients_snapshot:
            raise RuntimeError("No Google clients configured")
        for client in clients_snapshot:
            if not client.is_healthy():
                continue
            creds = self.get_credentials_for(client)
            try:
                # prefer passing credentials as kwarg
                if "credentials" in operation.__code__.co_varnames:
                    result = operation(*args, credentials=creds, **kwargs)
                else:
                    result = operation(creds, *args, **kwargs)
                self.mark_success(client.client_id)
                return result
            except Exception as exc:  # broad catch: caller should raise informative errors
                last_exc = exc
                msg = str(exc).lower()
                if "quota" in msg or "quota_exceeded" in msg:
                    self.mark_error(client.client_id, "quota")
                elif "rate" in msg or "rate_limit" in msg or "user rate" in msg:
                    self.mark_error(client.client_id, "rate_limit")
                else:
                    self.mark_error(client.client_id, "error")
                tried.append(client.client_id)
                # try next client
                continue
        # If we get here, all clients failed
        raise last_exc or RuntimeError("All Google clients failed")

    def test_client(self, client: GoogleClientRecord) -> bool:
        """Perform a lightweight API call to validate credentials.

        Returns True when client is usable.
        """
        try:
            creds = self.get_credentials_for(client)
            oauth2 = build("oauth2", "v2", credentials=creds)
            info = oauth2.userinfo().get().execute()
            return bool(info and info.get("email"))
        except Exception:
            return False


# Singleton manager
google_manager = GoogleClientManager()
