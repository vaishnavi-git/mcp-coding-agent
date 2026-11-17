"""Hidden acceptance tests for issue #43 (never shown to the agent)."""

import pytest

from authkit import InvalidToken, SessionStore


def test_logging_out_twice_does_not_raise():
    store = SessionStore()
    session = store.login("alice")
    store.logout(session.access_token)
    store.logout(session.access_token)


def test_logging_out_an_unknown_token_does_not_raise():
    SessionStore().logout("never-issued")


def test_logout_still_revokes_both_tokens():
    store = SessionStore()
    session = store.login("alice")
    store.logout(session.access_token)
    with pytest.raises(InvalidToken):
        store.authenticate(session.access_token)
    with pytest.raises(InvalidToken):
        store.refresh(session.refresh_token)
