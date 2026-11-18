import pytest

from shop import Cart, Money, Product, UnknownCoupon, totals

MUG = Product("mug", "Mug", Money.of("8.00"))
TEA = Product("tea", "Tea", Money.of("4.50"), taxable=False)
RED_SOCKS = Product("sock-red", "Red socks", Money.of("6.00"))


def test_tax_applies_to_taxable_products_only():
    cart = Cart()
    cart.add(MUG)
    cart.add(TEA)
    result = totals(cart)
    assert result.subtotal == Money.of("12.50")
    assert result.tax == Money.of("0.66")  # 8.25% of $8.00
    assert result.total == Money.of("13.16")


def test_coupon_takes_a_percentage_off_the_subtotal():
    cart = Cart()
    cart.add(MUG, 5)
    cart.apply_coupon("SAVE10")
    result = totals(cart)
    assert result.discount == Money.of("4.00")
    assert result.total == Money.of("40.00") - Money.of("4.00") + Money.of("3.30")


def test_unknown_coupons_are_refused():
    cart = Cart()
    cart.add(MUG)
    cart.apply_coupon("FREESTUFF")
    with pytest.raises(UnknownCoupon):
        totals(cart)


def test_buy_two_get_one_free():
    cart = Cart()
    cart.add(RED_SOCKS, 3)
    assert totals(cart).discount == Money.of("6.00")
