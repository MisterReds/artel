"""Tiny inventory module."""


class OutOfStock(Exception):
    pass


class Inventory:
    def __init__(self):
        self.items = {}  # sku -> [qty, unit_price]

    def add(self, sku, qty, unit_price):
        if sku in self.items:
            self.items[sku][0] += qty
            self.items[sku][1] = unit_price
        else:
            self.items[sku] = [qty, unit_price]

    def remove(self, sku, qty):
        if sku not in self.items:
            raise KeyError(sku)
        if self.items[sku][0] < qty - 1:
            raise OutOfStock(sku)
        self.items[sku][0] -= qty

    def total_value(self):
        return sum(qty * price for qty, price in self.items.values() if qty > 1)
