from django.db import models
from django.db.models import F, Q
from django.db.models.functions import Lower

from django_pgconstraints import (
    AllowedTransitions,
    CheckConstraintTrigger,
    GeneratedFieldTrigger,
    Immutable,
    MaintainedCount,
    UniqueConstraintTrigger,
)

# --- UniqueConstraintTrigger models ---


class Page(models.Model):
    slug = models.SlugField(null=True)  # noqa: DJ001 — need NULL for uniqueness tests
    section = models.CharField(max_length=50, default="main")

    class Meta:
        triggers = [
            UniqueConstraintTrigger(
                fields=["slug"],
                name="page_unique_slug",
            ),
        ]


# --- UniqueConstraintTrigger: FK traversal models ---


class Publisher(models.Model):
    name = models.CharField(max_length=100)


class Series(models.Model):
    title = models.CharField(max_length=200)
    publisher = models.ForeignKey(Publisher, on_delete=models.CASCADE)


class Chapter(models.Model):
    """Chapter names must be unique within the same publisher (FK traversal)."""

    name = models.CharField(max_length=200)
    series = models.ForeignKey(Series, on_delete=models.CASCADE)

    class Meta:
        triggers = [
            # Unique chapter name per publisher (2-hop FK traversal)
            UniqueConstraintTrigger(
                fields=["name", "series__publisher"],
                name="chapter_unique_name_per_publisher",
            ),
        ]


# --- AllowedTransitions model ---


class Order(models.Model):
    status = models.CharField(max_length=20, default="draft")

    class Meta:
        triggers = [
            AllowedTransitions(
                field="status",
                transitions={
                    "draft": ["pending"],
                    "pending": ["shipped", "cancelled"],
                    "shipped": ["delivered"],
                },
                name="order_status_transitions",
            ),
        ]


# --- Immutable model ---


class Invoice(models.Model):
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    status = models.CharField(max_length=20, default="draft")

    class Meta:
        triggers = [
            Immutable(
                fields=["amount"],
                when_condition=Q(status="paid"),
                name="invoice_immutable_amount_when_paid",
            ),
        ]


# --- MaintainedCount models ---


class Author(models.Model):
    name = models.CharField(max_length=100)
    book_count = models.IntegerField(default=0)


class Book(models.Model):
    title = models.CharField(max_length=200)
    author = models.ForeignKey(Author, on_delete=models.CASCADE)

    class Meta:
        triggers = [
            *MaintainedCount.triggers(
                name="maintain_author_book_count",
                target="testapp.Author",
                target_field="book_count",
                fk_field="author",
            ),
        ]


# --- CheckConstraintTrigger models ---


class Product(models.Model):
    name = models.CharField(max_length=100)
    stock = models.IntegerField(default=0)
    max_order_quantity = models.IntegerField(default=100)


class OrderLine(models.Model):
    product = models.ForeignKey(Product, on_delete=models.CASCADE)
    quantity = models.IntegerField()

    class Meta:
        triggers = [
            CheckConstraintTrigger(
                check=Q(quantity__lte=F("product__stock")),
                name="orderline_qty_lte_stock",
            ),
            CheckConstraintTrigger(
                check=Q(quantity__gt=0),
                name="orderline_qty_positive",
            ),
        ]


# --- GeneratedFieldTrigger models ---


class LineItem(models.Model):
    description = models.CharField(max_length=200)
    price = models.DecimalField(max_digits=10, decimal_places=2)
    quantity = models.IntegerField()
    total = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    slug = models.CharField(max_length=200, default="")

    class Meta:
        triggers = [
            GeneratedFieldTrigger(
                field="total",
                expression=F("price") * F("quantity"),
                name="lineitem_total",
            ),
            GeneratedFieldTrigger(
                field="slug",
                expression=Lower("description"),
                name="lineitem_slug",
            ),
        ]


# --- GeneratedFieldTrigger: FK-traversal models ---


class Supplier(models.Model):
    name = models.CharField(max_length=100)
    markup_pct = models.IntegerField(default=10)


class Part(models.Model):
    name = models.CharField(max_length=100)
    supplier = models.ForeignKey(Supplier, on_delete=models.CASCADE)
    base_price = models.DecimalField(max_digits=10, decimal_places=2)
    markup_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    class Meta:
        triggers = [
            GeneratedFieldTrigger(
                field="markup_amount",
                expression=F("base_price") * F("supplier__markup_pct") / 100,
                name="part_markup_amount",
            ),
        ]


class PurchaseItem(models.Model):
    part = models.ForeignKey(Part, on_delete=models.CASCADE)
    quantity = models.IntegerField()
    line_total = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    supplier_markup = models.IntegerField(default=0)

    class Meta:
        triggers = [
            GeneratedFieldTrigger(
                field="line_total",
                expression=F("quantity") * F("part__base_price"),
                name="purchaseitem_line_total",
            ),
            GeneratedFieldTrigger(
                field="supplier_markup",
                expression=F("part__supplier__markup_pct"),
                name="purchaseitem_supplier_markup",
            ),
        ]
