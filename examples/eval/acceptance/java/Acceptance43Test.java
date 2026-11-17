package authkit;

import static org.junit.jupiter.api.Assertions.assertDoesNotThrow;
import static org.junit.jupiter.api.Assertions.assertThrows;

import org.junit.jupiter.api.Test;

/** Hidden acceptance tests for issue #43 (never shown to the agent). */
class Acceptance43Test {

    @Test
    void loggingOutTwiceDoesNotThrow() {
        SessionStore store = new SessionStore();
        Session session = store.login("alice");
        store.logout(session.accessToken());
        assertDoesNotThrow(() -> store.logout(session.accessToken()));
    }

    @Test
    void loggingOutAnUnknownTokenDoesNotThrow() {
        assertDoesNotThrow(() -> new SessionStore().logout("never-issued"));
    }

    @Test
    void logoutStillRevokesBothTokens() {
        SessionStore store = new SessionStore();
        Session session = store.login("alice");
        store.logout(session.accessToken());
        assertThrows(InvalidTokenException.class, () -> store.authenticate(session.accessToken()));
        assertThrows(InvalidTokenException.class, () -> store.refresh(session.refreshToken()));
    }
}
