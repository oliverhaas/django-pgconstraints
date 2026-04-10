"""E-commerce models demonstrating all five constraint types.

UniqueConstraintTrigger — SKUs unique across Products and ArchivedProducts.
AllowedTransitions     — Orders follow a strict state machine.
Immutable              — Paid invoices cannot have their amount changed.
MaintainedCount        — Category.product_count stays in sync automatically.
CheckConstraintTrigger — OrderLine.quantity must not exceed Product.stock.
"""

from django.db import models
from django.db.models import F, Q

from django_pgconstraints import (
    AllowedTransitions,
    CheckConstraintTrigger,
    Immutable,
    MaintainedCount,
    UniqueConstraintTrigger,
)

# ---------------------------------------------------------------------------
# UniqueConstraintTrigger: cross-table SKU uniqueness
# ---------------------------------------------------------------------------


class Product(models.Model):
    name = models.CharField(max_length=200)
    sku = models.CharField(max_length=50, unique=True)
    stock = models.PositiveIntegerField(default=0)
    category = models.ForeignKey(
        "Category",
        on_delete=models.CASCADE,
        related_name="products",
    )

    class Meta:
        constraints = [
            UniqueConstraintTrigger(
                field="sku",
                across="shop.ArchivedProduct",
                name="shop_product_unique_sku_across_archived",
            ),
        ]

    def __str__(self):
        return f"{self.name} ({self.sku})"


class ArchivedProduct(models.Model):
    """Soft-deleted products kept for historical orders."""

    name = models.CharField(max_length=200)
    sku = models.CharField(max_length=50, unique=True)
    archived_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            UniqueConstraintTrigger(
                field="sku",
                across="shop.Product",
                name="shop_archived_unique_sku_across_product",
            ),
        ]

    def __str__(self):
        return f"{self.name} (archived)"


# ---------------------------------------------------------------------------
# MaintainedCount: automatic product_count on Category
# ---------------------------------------------------------------------------


class Category(models.Model):
    name = models.CharField(max_length=100)
    product_count = models.PositiveIntegerField(default=0)

    def __str__(self):
        return self.name


# The MaintainedCount is declared on the child model (Product) but acts on
# the parent (Category).  We add it here via Product.Meta.constraints.
Product._meta.constraints.append(  # noqa: SLF001
    MaintainedCount(
        target="shop.Category",
        target_field="product_count",
        fk_field="category",
        name="shop_maintain_category_product_count",
    ),
)


# ---------------------------------------------------------------------------
# AllowedTransitions: order state machine
# ---------------------------------------------------------------------------


class Order(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft"
        PENDING = "pending"
        PAID = "paid"
        SHIPPED = "shipped"
        DELIVERED = "delivered"
        CANCELLED = "cancelled"

    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.DRAFT,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            AllowedTransitions(
                field="status",
                transitions={
                    "draft": ["pending", "cancelled"],
                    "pending": ["paid", "cancelled"],
                    "paid": ["shipped", "cancelled"],
                    "shipped": ["delivered"],
                },
                name="shop_order_status_transitions",
            ),
        ]

    def __str__(self):
        return f"Order #{self.pk} ({self.status})"


# ---------------------------------------------------------------------------
# Immutable: lock invoice amount once paid
# ---------------------------------------------------------------------------


class Invoice(models.Model):
    order = models.OneToOneField(Order, on_delete=models.CASCADE)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    status = models.CharField(max_length=20, default="draft")

    class Meta:
        constraints = [
            Immutable(
                fields=["amount"],
                when=Q(status="paid"),
                name="shop_invoice_immutable_amount_when_paid",
            ),
        ]

    def __str__(self):
        return f"Invoice #{self.pk} — {self.amount}"


# ---------------------------------------------------------------------------
# CheckConstraintTrigger: quantity must not exceed stock
# ---------------------------------------------------------------------------


class OrderLine(models.Model):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="lines")
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    quantity = models.PositiveIntegerField()

    class Meta:
        constraints = [
            CheckConstraintTrigger(
                check=Q(quantity__lte=F("product__stock")),
                name="shop_orderline_qty_lte_stock",
            ),
            CheckConstraintTrigger(
                check=Q(quantity__gt=0),
                name="shop_orderline_qty_positive",
            ),
        ]

    def __str__(self):
        return f"{self.quantity}x {self.product.name}"
