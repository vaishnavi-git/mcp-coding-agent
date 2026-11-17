package authkit;

/** Thrown when a token is unknown, expired or already used. */
public class InvalidTokenException extends RuntimeException {
    public InvalidTokenException(String message) {
        super(message);
    }
}
