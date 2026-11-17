package prefetch;

import static org.junit.jupiter.api.Assertions.assertEquals;

import org.junit.jupiter.api.Test;

class GreetingTest {
    @Test
    void saysHello() {
        assertEquals("hello", Greeting.hello());
    }
}
