"""Amounts of money in one currency, kept as whole cents so nothing is lost to floating point."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal


def _round_cents(value: Decimal) -> int:
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


@dataclass(frozen=True, order=True)
class Money:
    cents: int

    @classmethod
    def of(cls, amount: str | int | Decimal) -> Money:
        """Money.of("19.99") is 1999 cents. Pass strings, not floats."""
        return cls(_round_cents(Decimal(str(amount)) * 100))

    def __add__(self, other: Money) -> Money:
        return Money(self.cents + other.cents)

    def __sub__(self, other: Money) -> Money:
        return Money(self.cents - other.cents)

    def times(self, quantity: int) -> Money:
        return Money(self.cents * quantity)

    def percent(self, rate: str | Decimal) -> Money:
        """This amount times ``rate`` percent, rounded half up to the cent."""
        return Money(_round_cents(Decimal(self.cents) * Decimal(str(rate)) / 100))

    def __str__(self) -> str:
        sign = "-" if self.cents < 0 else ""
        cents = abs(self.cents)
        return f"{sign}${cents // 100}.{cents % 100:02d}"


ZERO = Money(0)
