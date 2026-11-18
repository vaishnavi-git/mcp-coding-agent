"""Coupons and promotions. Each function returns the discount as a positive amount."""

from __future__ import annotations

from decimal import Decimal

from .cart import Cart
from .money import ZERO, Money

# Coupon code -> percent off the subtotal.
COUPONS: dict[str, Decimal] = {"SAVE10": Decimal("10"), "WELCOME5": Decimal("5")}

# Products in the "buy 2, get 1 free" promotion.
THREE_FOR_TWO_SKUS = frozenset({"sock-red", "sock-blue", "sock-wool", "sock-trainer"})


class UnknownCoupon(ValueError):
    """The customer entered a coupon code that doesn't exist."""


def coupon_discount(cart: Cart) -> Money:
    subtotal = cart.subtotal()
    discount = ZERO
    for code in cart.coupons:
        rate = COUPONS.get(code)
        if rate is None:
            raise UnknownCoupon(code)
        discount += subtotal.percent(rate)
    return discount


def three_for_two_discount(cart: Cart) -> Money:
    """Buy 2, get 1 free on the promotion's products: one item free for every three bought."""
    units = [line.product.price for line in cart.lines if line.product.sku in THREE_FOR_TWO_SKUS for _ in range(line.quantity)]
    free = len(units) // 3
    return sum(units[:free], ZERO)
