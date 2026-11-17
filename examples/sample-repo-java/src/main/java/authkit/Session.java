package authkit;

/** One login: an access token, a refresh token and when each expires (seconds since the epoch). */
public record Session(
        String userId,
        String accessToken,
        String refreshToken,
        long accessExpiresAt,
        long refreshExpiresAt) {}
