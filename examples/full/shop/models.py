"""A small inventory / purchase order domain exercising all three triggers.

``Supplier`` owns many ``Part``s, and a ``PurchaseOrder`` has many ``PurchaseLine``s.
Each trigger demonstrates a rule that Django's built-in constraints can't express
because they all cross a foreign-key boundary.

| Trigger                    | Rule                                                  |
|----------------------------|-------------------------------------------------------|
| ``UniqueConstraintTrigger``| ``Part.sku`` must be unique *within* each supplier    |
| ``CheckConstraintTrigger`` | ``PurchaseLine.quantity`` must not exceed ``part.stock`` |
| ``GeneratedFieldTrigger``  | ``Part.markup_amount`` is derived from the supplier's markup_pct |
| ``GeneratedFieldTrigger``  | ``PurchaseLine.line_total`` is derived from ``quantity * part.base_price`` |
"""

from django.db import models
from django.db.models import F, Q

from django_pgconstraints import (
    CheckConstraintTrigger,
    GeneratedFieldTrigger,
    UniqueConstraintTrigger,
)


class Supplier(models.Model):
    name = models.CharField(max_length=100, unique=True)
    markup_pct = models.IntegerField(default=10)

    def __str__(self) -> str:
        return self.name


class Part(models.Model):
    sku = models.CharField(max_length=50)
    name = models.CharField(max_length=200)
    supplier = models.ForeignKey(Supplier, on_delete=models.CASCADE, related_name="parts")
    base_price = models.DecimalField(max_digits=10, decimal_places=2)
    stock = models.IntegerField(default=0)
    # Populated by the trigger below.  Keep a sensible default so inserts
    # that don't set it explicitly are valid until the trigger runs.
    markup_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    class Meta:
        triggers = [
            UniqueConstraintTrigger(
                fields=["sku", "supplier"],
                name="part_unique_sku_per_supplier",
            ),
            GeneratedFieldTrigger(
                field="markup_amount",
                expression=F("base_price") * F("supplier__markup_pct") / 100,
                name="part_markup_amount",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.sku} ({self.name})"


class PurchaseOrder(models.Model):
    reference = models.CharField(max_length=50, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return self.reference


class PurchaseLine(models.Model):
    order = models.ForeignKey(PurchaseOrder, on_delete=models.CASCADE, related_name="lines")
    part = models.ForeignKey(Part, on_delete=models.PROTECT)
    quantity = models.IntegerField()
    line_total = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    class Meta:
        triggers = [
            CheckConstraintTrigger(
                condition=Q(quantity__gt=0),
                name="purchaseline_qty_positive",
            ),
            CheckConstraintTrigger(
                condition=Q(quantity__lte=F("part__stock")),
                name="purchaseline_qty_lte_stock",
            ),
            GeneratedFieldTrigger(
                field="line_total",
                expression=F("quantity") * F("part__base_price"),
                name="purchaseline_line_total",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.quantity}x {self.part.sku}"
