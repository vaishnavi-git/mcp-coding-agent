"""The totals a customer sees at checkout."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .cart import Cart
from .discounts import coupon_discount, three_for_two_discount
from .money import ZERO, Money

TAX_RATE = Decimal("8.25")  # percent, on taxable products


@dataclass(frozen=True)
class Totals:
    subtotal: Money
    discount: Money
    tax: Money
    total: Money


def totals(cart: Cart) -> Totals:
    subtotal = cart.subtotal()
    discount = coupon_discount(cart) + three_for_two_discount(cart)
    tax = ZERO
    for line in cart.lines:
        if line.product.taxable:
            tax += line.total.percent(TAX_RATE)
    return Totals(subtotal, discount, tax, subtotal - discount + tax)
