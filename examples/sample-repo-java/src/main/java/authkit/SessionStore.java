package authkit;

import java.security.SecureRandom;
import java.util.Base64;
import java.util.HashMap;
import java.util.Map;
import java.util.function.LongSupplier;

/** In-memory session store with short-lived access tokens and rotating refresh tokens. */
public class SessionStore {
    public static final long ACCESS_TOKEN_TTL = 15 * 60; // seconds
    public static final long REFRESH_TOKEN_TTL = 7 * 24 * 60 * 60; // seconds

    private static final SecureRandom RANDOM = new SecureRandom();

    private final LongSupplier clock;
    private final Map<String, Session> byAccess = new HashMap<>();
    private final Map<String, Session> byRefresh = new HashMap<>();

    public SessionStore() {
        this(() -> System.currentTimeMillis() / 1000);
    }

    /** @param clock returns the current time in seconds */
    public SessionStore(LongSupplier clock) {
        this.clock = clock;
    }

    private static String newToken(int bytes) {
        byte[] buffer = new byte[bytes];
        RANDOM.nextBytes(buffer);
        return Base64.getUrlEncoder().withoutPadding().encodeToString(buffer);
    }

    private Session newSession(String userId) {
        long now = clock.getAsLong();
        return new Session(userId, newToken(24), newToken(32), now + ACCESS_TOKEN_TTL, now + REFRESH_TOKEN_TTL);
    }

    public Session login(String userId) {
        Session session = newSession(userId);
        byAccess.put(session.accessToken(), session);
        byRefresh.put(session.refreshToken(), session);
        return session;
    }

    /** Returns the user id for a valid access token. */
    public String authenticate(String accessToken) {
        Session session = byAccess.get(accessToken);
        if (session == null || session.accessExpiresAt() <= clock.getAsLong()) {
            throw new InvalidTokenException("access token is invalid or expired");
        }
        return session.userId();
    }

    /** Exchanges a refresh token for a new session. Refresh tokens are single-use. */
    public Session refresh(String refreshToken) {
        Session session = byRefresh.get(refreshToken);
        if (session == null || session.refreshExpiresAt() <= clock.getAsLong()) {
            throw new InvalidTokenException("refresh token is invalid or expired");
        }
        Session newSession = newSession(session.userId());
        byAccess.remove(session.accessToken());
        byAccess.put(newSession.accessToken(), newSession);
        return newSession;
    }

    public void logout(String accessToken) {
        Session session = byAccess.remove(accessToken);
        if (session != null) {
            byRefresh.remove(session.refreshToken());
        }
    }
}
