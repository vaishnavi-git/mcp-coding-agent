"""Hidden acceptance tests for shop issue #7 (never shown to the agent)."""

from shop import Cart, Money, Product, totals

MUG = Product("mug", "Mug", Money.of("8.00"))
PENS = [Product(f"pen-{colour}", f"{colour.title()} pen", Money.of("0.10")) for colour in ("blue", "red", "black")]
TEA = Product("tea", "Tea", Money.of("4.50"), taxable=False)


def with_pens(cart: Cart) -> Cart:
    for pen in PENS:
        cart.add(pen)
    return cart


def test_tax_is_rounded_once_on_the_taxable_amount():
    cart = with_pens(Cart())
    assert totals(cart).tax == Money.of("0.02")
    assert totals(cart).total == Money.of("0.32")


def test_mixed_cart_from_the_issue():
    cart = Cart()
    cart.add(MUG)
    with_pens(cart)
    cart.add(TEA)
    assert totals(cart).tax == Money.of("0.68")  # 8.25% of $8.30, untaxed tea left out


def test_single_line_orders_are_unchanged():
    cart = Cart()
    cart.add(MUG)
    assert totals(cart).tax == Money.of("0.66")


def test_money_percent_still_rounds_half_up():
    # The report blamed Money.percent; its rounding was right and coupons rely on it.
    assert Money.of("19.99").percent("8.25") == Money(165)
    assert Money(2).percent("25") == Money(1)
