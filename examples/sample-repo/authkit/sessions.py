"""In-memory session store with short-lived access tokens and rotating refresh tokens."""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from typing import Callable

ACCESS_TOKEN_TTL = 15 * 60  # seconds
REFRESH_TOKEN_TTL = 7 * 24 * 60 * 60  # seconds


class InvalidToken(Exception):
    """Raised when a token is unknown, expired or already used."""


@dataclass(frozen=True)
class Session:
    user_id: str
    access_token: str
    refresh_token: str
    access_expires_at: float
    refresh_expires_at: float


class SessionStore:
    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._by_access: dict[str, Session] = {}
        self._by_refresh: dict[str, Session] = {}

    def _new_session(self, user_id: str) -> Session:
        now = self._clock()
        return Session(
            user_id=user_id,
            access_token=secrets.token_urlsafe(24),
            refresh_token=secrets.token_urlsafe(32),
            access_expires_at=now + ACCESS_TOKEN_TTL,
            refresh_expires_at=now + REFRESH_TOKEN_TTL,
        )

    def login(self, user_id: str) -> Session:
        session = self._new_session(user_id)
        self._by_access[session.access_token] = session
        self._by_refresh[session.refresh_token] = session
        return session

    def authenticate(self, access_token: str) -> str:
        """Return the user id for a valid access token."""
        session = self._by_access.get(access_token)
        if session is None or session.access_expires_at <= self._clock():
            raise InvalidToken("access token is invalid or expired")
        return session.user_id

    def refresh(self, refresh_token: str) -> Session:
        """Exchange a refresh token for a new session. Refresh tokens are single-use."""
        session = self._by_refresh.get(refresh_token)
        if session is None or session.refresh_expires_at <= self._clock():
            raise InvalidToken("refresh token is invalid or expired")
        new_session = self._new_session(session.user_id)
        self._by_access.pop(session.access_token, None)
        self._by_access[new_session.access_token] = new_session
        return new_session

    def logout(self, access_token: str) -> None:
        session = self._by_access.pop(access_token, None)
        if session is not None:
            self._by_refresh.pop(session.refresh_token, None)
