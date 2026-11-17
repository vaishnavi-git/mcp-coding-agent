package authkit;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;

import org.junit.jupiter.api.Test;

/** Hidden acceptance tests for issue #42 (never shown to the agent). */
class Acceptance42Test {

    @Test
    void theNewRefreshTokenWorksForTheNextRefresh() {
        SessionStore store = new SessionStore();
        Session first = store.login("alice");
        Session second = store.refresh(first.refreshToken());
        Session third = store.refresh(second.refreshToken());
        assertEquals("alice", store.authenticate(third.accessToken()));
    }

    @Test
    void aUsedRefreshTokenIsRejected() {
        SessionStore store = new SessionStore();
        Session first = store.login("alice");
        store.refresh(first.refreshToken());
        assertThrows(InvalidTokenException.class, () -> store.refresh(first.refreshToken()));
    }

    @Test
    void logoutAfterARefreshRevokesTheNewRefreshToken() {
        SessionStore store = new SessionStore();
        Session refreshed = store.refresh(store.login("alice").refreshToken());
        store.logout(refreshed.accessToken());
        assertThrows(InvalidTokenException.class, () -> store.refresh(refreshed.refreshToken()));
    }
}
