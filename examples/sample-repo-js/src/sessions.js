'use strict';
// In-memory session store with short-lived access tokens and rotating refresh tokens.

const crypto = require('node:crypto');

const ACCESS_TOKEN_TTL = 15 * 60; // seconds
const REFRESH_TOKEN_TTL = 7 * 24 * 60 * 60; // seconds

/** Thrown when a token is unknown, expired or already used. */
class InvalidToken extends Error {
  constructor(message) {
    super(message);
    this.name = 'InvalidToken';
  }
}

function newToken(bytes) {
  return crypto.randomBytes(bytes).toString('base64url');
}

class SessionStore {
  /** @param {{clock?: () => number}} [options] clock returns the current time in seconds */
  constructor({ clock = () => Date.now() / 1000 } = {}) {
    this._clock = clock;
    this._byAccess = new Map();
    this._byRefresh = new Map();
  }

  _newSession(userId) {
    const now = this._clock();
    return Object.freeze({
      userId,
      accessToken: newToken(24),
      refreshToken: newToken(32),
      accessExpiresAt: now + ACCESS_TOKEN_TTL,
      refreshExpiresAt: now + REFRESH_TOKEN_TTL,
    });
  }

  login(userId) {
    const session = this._newSession(userId);
    this._byAccess.set(session.accessToken, session);
    this._byRefresh.set(session.refreshToken, session);
    return session;
  }

  /** Returns the user id for a valid access token. */
  authenticate(accessToken) {
    const session = this._byAccess.get(accessToken);
    if (session === undefined || session.accessExpiresAt <= this._clock()) {
      throw new InvalidToken('access token is invalid or expired');
    }
    return session.userId;
  }

  /** Exchanges a refresh token for a new session. Refresh tokens are single-use. */
  refresh(refreshToken) {
    const session = this._byRefresh.get(refreshToken);
    if (session === undefined || session.refreshExpiresAt <= this._clock()) {
      throw new InvalidToken('refresh token is invalid or expired');
    }
    const newSession = this._newSession(session.userId);
    this._byAccess.delete(session.accessToken);
    this._byAccess.set(newSession.accessToken, newSession);
    return newSession;
  }

  logout(accessToken) {
    const session = this._byAccess.get(accessToken);
    this._byAccess.delete(accessToken);
    if (session !== undefined) {
      this._byRefresh.delete(session.refreshToken);
    }
  }
}

module.exports = { ACCESS_TOKEN_TTL, REFRESH_TOKEN_TTL, InvalidToken, SessionStore };
