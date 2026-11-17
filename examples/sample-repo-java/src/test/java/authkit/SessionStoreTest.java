package authkit;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;

import java.util.function.LongSupplier;
import org.junit.jupiter.api.Test;

class SessionStoreTest {

    private static final class FakeClock implements LongSupplier {
        long now = 1_000_000;

        @Override
        public long getAsLong() {
            return now;
        }
    }

    @Test
    void loginReturnsWorkingAccessToken() {
        SessionStore store = new SessionStore();
        Session session = store.login("alice");
        assertEquals("alice", store.authenticate(session.accessToken()));
    }

    @Test
    void expiredAccessTokenIsRejected() {
        FakeClock clock = new FakeClock();
        SessionStore store = new SessionStore(clock);
        Session session = store.login("alice");
        clock.now += SessionStore.ACCESS_TOKEN_TTL + 1;
        assertThrows(InvalidTokenException.class, () -> store.authenticate(session.accessToken()));
    }

    @Test
    void refreshIssuesNewAccessToken() {
        SessionStore store = new SessionStore();
        Session session = store.login("alice");
        Session refreshed = store.refresh(session.refreshToken());
        assertNotEquals(session.accessToken(), refreshed.accessToken());
        assertEquals("alice", store.authenticate(refreshed.accessToken()));
        assertThrows(InvalidTokenException.class, () -> store.authenticate(session.accessToken()));
    }

    @Test
    void refreshRotatesRefreshToken() {
        SessionStore store = new SessionStore();
        Session session = store.login("alice");
        store.refresh(session.refreshToken());
        // a used refresh token must not work again
        assertThrows(InvalidTokenException.class, () -> store.refresh(session.refreshToken()));
    }

    @Test
    void logoutRevokesTokens() {
        SessionStore store = new SessionStore();
        Session session = store.login("alice");
        store.logout(session.accessToken());
        assertThrows(InvalidTokenException.class, () -> store.authenticate(session.accessToken()));
        assertThrows(InvalidTokenException.class, () -> store.refresh(session.refreshToken()));
    }
}
