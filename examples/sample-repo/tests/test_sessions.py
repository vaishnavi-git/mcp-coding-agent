import pytest

from authkit import InvalidToken, SessionStore
from authkit.sessions import ACCESS_TOKEN_TTL


class FakeClock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def test_login_returns_working_access_token():
    store = SessionStore()
    session = store.login("alice")
    assert store.authenticate(session.access_token) == "alice"


def test_expired_access_token_is_rejected():
    clock = FakeClock()
    store = SessionStore(clock=clock)
    session = store.login("alice")
    clock.now += ACCESS_TOKEN_TTL + 1
    with pytest.raises(InvalidToken):
        store.authenticate(session.access_token)


def test_refresh_issues_new_access_token():
    store = SessionStore()
    session = store.login("alice")
    refreshed = store.refresh(session.refresh_token)
    assert refreshed.access_token != session.access_token
    assert store.authenticate(refreshed.access_token) == "alice"
    with pytest.raises(InvalidToken):
        store.authenticate(session.access_token)


def test_refresh_rotates_refresh_token():
    store = SessionStore()
    session = store.login("alice")
    store.refresh(session.refresh_token)
    with pytest.raises(InvalidToken):
        store.refresh(session.refresh_token)  # a used refresh token must not work again


def test_logout_revokes_tokens():
    store = SessionStore()
    session = store.login("alice")
    store.logout(session.access_token)
    with pytest.raises(InvalidToken):
        store.authenticate(session.access_token)
    with pytest.raises(InvalidToken):
        store.refresh(session.refresh_token)
