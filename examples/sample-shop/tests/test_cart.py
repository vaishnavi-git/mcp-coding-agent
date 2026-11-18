import pytest

from shop import Cart, Money, Product

MUG = Product("mug", "Mug", Money.of("8.00"))
TEA = Product("tea", "Tea", Money.of("4.50"), taxable=False)


def test_adding_the_same_product_again_increases_the_quantity():
    cart = Cart()
    cart.add(MUG)
    cart.add(MUG, 2)
    assert [(line.product.sku, line.quantity) for line in cart.lines] == [("mug", 3)]
    assert cart.subtotal() == Money.of("24.00")


def test_quantity_must_be_positive():
    with pytest.raises(ValueError):
        Cart().add(MUG, 0)


def test_remove_drops_the_line():
    cart = Cart()
    cart.add(MUG)
    cart.add(TEA, 2)
    cart.remove("mug")
    assert cart.subtotal() == Money.of("9.00")
