"""Hidden acceptance tests for shop issue #8 (never shown to the agent)."""

import pytest

from shop import Cart, Money, Product, UnknownCoupon, totals

MUG = Product("mug", "Mug", Money.of("10.00"), taxable=False)


def cart_with(*codes: str) -> Cart:
    cart = Cart()
    cart.add(MUG, 10)
    for code in codes:
        cart.apply_coupon(code)
    return cart


def test_the_same_code_twice_counts_once():
    assert totals(cart_with("SAVE10", "SAVE10")).discount == Money.of("10.00")


@pytest.mark.parametrize("code", ["save10", " SAVE10", "Save10 ", "\tsave10\n"])
def test_codes_ignore_case_and_surrounding_spaces(code):
    assert totals(cart_with(code)).discount == Money.of("10.00")


def test_different_spellings_of_one_code_count_once():
    assert totals(cart_with("save10", "SAVE10 ")).discount == Money.of("10.00")


def test_different_codes_still_stack():
    assert totals(cart_with("SAVE10", "welcome5")).discount == Money.of("15.00")


def test_unknown_codes_are_still_refused():
    with pytest.raises(UnknownCoupon):
        totals(cart_with("FREESTUFF"))
