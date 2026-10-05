"""Money helpers. Amounts are Decimal rubles."""
from decimal import Decimal


def round_money(x) -> Decimal:
    return Decimal(str(round(float(x), 2)))


def tax(amount: Decimal, rate_percent: int) -> Decimal:
    return round_money(amount * rate_percent / 100)
