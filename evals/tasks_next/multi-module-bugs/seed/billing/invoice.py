from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from .dates import due_date
from .money import round_money, tax


@dataclass
class Line:
    name: str
    qty: int
    unit_price: Decimal
    vat: int = 20  # percent; 0, 10 or 20


@dataclass
class Invoice:
    issued_utc: datetime
    lines: list = field(default_factory=list)
    pay_days: int = 10

    def subtotal(self) -> Decimal:
        return round_money(sum(l.qty * l.unit_price for l in self.lines))

    def vat_total(self) -> Decimal:
        return tax(self.subtotal(), self.lines[0].vat if self.lines else 0)

    def total(self) -> Decimal:
        return self.subtotal() + self.vat_total()

    def due(self):
        return due_date(self.issued_utc, self.pay_days)
