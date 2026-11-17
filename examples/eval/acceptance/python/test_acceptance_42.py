"""Hidden acceptance tests for issue #42 (never shown to the agent)."""

import pytest

from authkit import InvalidToken, SessionStore


def test_the_new_refresh_token_works_for_the_next_refresh():
    store = SessionStore()
    first = store.login("alice")
    second = store.refresh(first.refresh_token)
    third = store.refresh(second.refresh_token)
    assert store.authenticate(third.access_token) == "alice"


def test_a_used_refresh_token_is_rejected():
    store = SessionStore()
    first = store.login("alice")
    store.refresh(first.refresh_token)
    with pytest.raises(InvalidToken):
        store.refresh(first.refresh_token)


def test_logout_after_a_refresh_revokes_the_new_refresh_token():
    store = SessionStore()
    refreshed = store.refresh(store.login("alice").refresh_token)
    store.logout(refreshed.access_token)
    with pytest.raises(InvalidToken):
        store.refresh(refreshed.refresh_token)
