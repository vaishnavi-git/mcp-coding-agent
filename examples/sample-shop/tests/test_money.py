from shop import Money


def test_amounts_are_exact_cents():
    assert Money.of("19.99").cents == 1999
    assert Money.of("0.1") + Money.of("0.2") == Money.of("0.3")
    assert Money.of(5).times(3) == Money.of("15.00")


def test_percent_rounds_half_up_to_the_cent():
    assert Money.of("19.99").percent("8.25") == Money(165)  # 164.9175 cents
    assert Money(2).percent("25") == Money(1)  # 0.5 cents rounds up
    assert Money(1).percent("25") == Money(0)


def test_amounts_print_as_dollars():
    assert str(Money.of("1234.5")) == "$1234.50"
    assert str(Money(-5)) == "-$0.05"
