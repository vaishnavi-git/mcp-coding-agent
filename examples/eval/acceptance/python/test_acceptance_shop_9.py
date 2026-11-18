"""Hidden acceptance tests for shop issue #9 (never shown to the agent)."""

from shop import Cart, Money, Product, totals

RED = Product("sock-red", "Red socks", Money.of("6.00"))
WOOL = Product("sock-wool", "Wool socks", Money.of("15.00"))
TRAINER = Product("sock-trainer", "Trainer socks", Money.of("4.00"))
MUG = Product("mug", "Mug", Money.of("2.00"))


def discount(*lines: tuple[Product, int]) -> Money:
    cart = Cart()
    for product, quantity in lines:
        cart.add(product, quantity)
    return totals(cart).discount


def test_the_cheapest_item_is_free():
    assert discount((RED, 1), (WOOL, 2)) == Money.of("6.00")


def test_the_order_items_were_added_in_does_not_matter():
    assert discount((WOOL, 2), (RED, 1)) == Money.of("6.00")
    assert discount((WOOL, 1), (RED, 1), (WOOL, 1)) == Money.of("6.00")


def test_six_items_make_the_two_cheapest_free():
    assert discount((WOOL, 3), (RED, 3)) == Money.of("12.00")


def test_quantities_on_one_line_count_as_separate_items():
    assert discount((RED, 1), (TRAINER, 3)) == Money.of("4.00")


def test_products_outside_the_promotion_are_ignored():
    assert discount((MUG, 5), (RED, 2)) == Money.of("0.00")
    assert discount((MUG, 1), (RED, 2), (WOOL, 1)) == Money.of("6.00")
