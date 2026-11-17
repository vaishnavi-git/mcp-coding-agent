'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');

const { ACCESS_TOKEN_TTL, InvalidToken, SessionStore } = require('../src');

function fakeClock(now = 1_000_000) {
  const clock = () => clock.now;
  clock.now = now;
  return clock;
}

test('login returns a working access token', () => {
  const store = new SessionStore();
  const session = store.login('alice');
  assert.equal(store.authenticate(session.accessToken), 'alice');
});

test('an expired access token is rejected', () => {
  const clock = fakeClock();
  const store = new SessionStore({ clock });
  const session = store.login('alice');
  clock.now += ACCESS_TOKEN_TTL + 1;
  assert.throws(() => store.authenticate(session.accessToken), InvalidToken);
});

test('refresh issues a new access token', () => {
  const store = new SessionStore();
  const session = store.login('alice');
  const refreshed = store.refresh(session.refreshToken);
  assert.notEqual(refreshed.accessToken, session.accessToken);
  assert.equal(store.authenticate(refreshed.accessToken), 'alice');
  assert.throws(() => store.authenticate(session.accessToken), InvalidToken);
});

test('refresh rotates the refresh token', () => {
  const store = new SessionStore();
  const session = store.login('alice');
  store.refresh(session.refreshToken);
  // a used refresh token must not work again
  assert.throws(() => store.refresh(session.refreshToken), InvalidToken);
});

test('logout revokes tokens', () => {
  const store = new SessionStore();
  const session = store.login('alice');
  store.logout(session.accessToken);
  assert.throws(() => store.authenticate(session.accessToken), InvalidToken);
  assert.throws(() => store.refresh(session.refreshToken), InvalidToken);
});
