"""A shopping cart: product lines with quantities, and the coupons the customer entered."""

from __future__ import annotations

from dataclasses import dataclass

from .catalog import Product
from .money import ZERO, Money


@dataclass
class Line:
    product: Product
    quantity: int

    @property
    def total(self) -> Money:
        return self.product.price.times(self.quantity)


class Cart:
    def __init__(self) -> None:
        self.lines: list[Line] = []
        self.coupons: list[str] = []

    def add(self, product: Product, quantity: int = 1) -> None:
        if quantity < 1:
            raise ValueError("quantity must be at least 1")
        for line in self.lines:
            if line.product.sku == product.sku:
                line.quantity += quantity
                return
        self.lines.append(Line(product, quantity))

    def remove(self, sku: str) -> None:
        self.lines = [line for line in self.lines if line.product.sku != sku]

    def apply_coupon(self, code: str) -> None:
        self.coupons.append(code)

    def subtotal(self) -> Money:
        return sum((line.total for line in self.lines), ZERO)
