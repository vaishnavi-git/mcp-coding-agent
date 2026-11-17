'use strict';
// Hidden acceptance tests for issue #43 (never shown to the agent).

const { test } = require('node:test');
const assert = require('node:assert/strict');

const { InvalidToken, SessionStore } = require('../src');

test('logging out twice does not throw', () => {
  const store = new SessionStore();
  const session = store.login('alice');
  store.logout(session.accessToken);
  assert.doesNotThrow(() => store.logout(session.accessToken));
});

test('logging out an unknown token does not throw', () => {
  assert.doesNotThrow(() => new SessionStore().logout('never-issued'));
});

test('logout still revokes both tokens', () => {
  const store = new SessionStore();
  const session = store.login('alice');
  store.logout(session.accessToken);
  assert.throws(() => store.authenticate(session.accessToken), InvalidToken);
  assert.throws(() => store.refresh(session.refreshToken), InvalidToken);
});
