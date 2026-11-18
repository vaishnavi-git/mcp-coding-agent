"""Products and the catalog they are looked up in."""

from __future__ import annotations

from dataclasses import dataclass

from .money import Money


@dataclass(frozen=True)
class Product:
    sku: str
    name: str
    price: Money
    taxable: bool = True


class Catalog:
    def __init__(self, products: list[Product] | None = None) -> None:
        self._by_sku: dict[str, Product] = {}
        for product in products or []:
            self.add(product)

    def add(self, product: Product) -> None:
        if product.sku in self._by_sku:
            raise ValueError(f"duplicate sku {product.sku!r}")
        self._by_sku[product.sku] = product

    def get(self, sku: str) -> Product:
        try:
            return self._by_sku[sku]
        except KeyError:
            raise KeyError(f"unknown sku {sku!r}") from None
