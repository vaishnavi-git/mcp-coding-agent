'use strict';
// Hidden acceptance tests for issue #42 (never shown to the agent).

const { test } = require('node:test');
const assert = require('node:assert/strict');

const { InvalidToken, SessionStore } = require('../src');

test('the new refresh token works for the next refresh', () => {
  const store = new SessionStore();
  const first = store.login('alice');
  const second = store.refresh(first.refreshToken);
  const third = store.refresh(second.refreshToken);
  assert.equal(store.authenticate(third.accessToken), 'alice');
});

test('a used refresh token is rejected', () => {
  const store = new SessionStore();
  const first = store.login('alice');
  store.refresh(first.refreshToken);
  assert.throws(() => store.refresh(first.refreshToken), InvalidToken);
});

test('logout after a refresh revokes the new refresh token', () => {
  const store = new SessionStore();
  const refreshed = store.refresh(store.login('alice').refreshToken);
  store.logout(refreshed.accessToken);
  assert.throws(() => store.refresh(refreshed.refreshToken), InvalidToken);
});
