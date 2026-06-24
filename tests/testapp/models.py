from django.db import models
from django.db.models import F, Q, Sum
from django.db.models.functions import Lower

from django_pgconstraints import (
    CheckConstraintTrigger,
    GeneratedFieldTrigger,
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
            UniqueConstraintTrigger(
                fields=["name", "series__publisher"],
                name="chapter_unique_name_per_publisher",
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
                condition=Q(quantity__lte=F("product__stock")),
                name="orderline_qty_lte_stock",
            ),
            CheckConstraintTrigger(
                condition=Q(quantity__gt=0),
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


# --- GeneratedFieldTrigger: auto_refresh=False opt-out ---


class ManualRefreshItem(models.Model):
    """Opt-out of RETURNING-based auto-refresh for the computed field."""

    price = models.DecimalField(max_digits=10, decimal_places=2)
    quantity = models.IntegerField()
    total = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    class Meta:
        triggers = [
            GeneratedFieldTrigger(
                field="total",
                expression=F("price") * F("quantity"),
                auto_refresh=False,
                name="manualrefreshitem_total",
            ),
        ]


# --- GeneratedFieldTrigger: aggregate over reverse relation ---


class Invoice(models.Model):
    """Parent of InvoiceLine. ``total`` is the SUM of related line amounts."""

    total = models.IntegerField(default=0)

    class Meta:
        triggers = [
            GeneratedFieldTrigger(
                field="total",
                expression=Sum("lines__amount"),
                name="invoice_total",
            ),
        ]


class InvoiceLine(models.Model):
    invoice = models.ForeignKey(Invoice, related_name="lines", on_delete=models.CASCADE)
    amount = models.IntegerField()
    # Non-aggregated column used in tests to verify that UPDATEs touching
    # only this column don't fire the parent recompute.
    note = models.CharField(max_length=100, default="")


# --- GeneratedFieldTrigger: multi-hop aggregate over reverse relations ---


class Customer(models.Model):
    """Two hops up from CartItem.amount via Cart."""

    name = models.CharField(max_length=100)
    lifetime_total = models.IntegerField(default=0)

    class Meta:
        triggers = [
            GeneratedFieldTrigger(
                field="lifetime_total",
                expression=Sum("carts__items__amount"),
                name="customer_lifetime_total",
            ),
        ]


class Cart(models.Model):
    customer = models.ForeignKey(Customer, related_name="carts", on_delete=models.CASCADE)


class CartItem(models.Model):
    cart = models.ForeignKey(Cart, related_name="items", on_delete=models.CASCADE)
    amount = models.IntegerField()
    note = models.CharField(max_length=100, default="")


# --- GeneratedFieldTrigger: 3-hop aggregate (Tenant → Account → Subscription → Charge) ---


class Tenant(models.Model):
    name = models.CharField(max_length=100)
    lifetime_revenue = models.IntegerField(default=0)

    class Meta:
        triggers = [
            GeneratedFieldTrigger(
                field="lifetime_revenue",
                expression=Sum("accounts__subscriptions__charges__amount"),
                name="tenant_lifetime_revenue",
            ),
        ]


class Account(models.Model):
    tenant = models.ForeignKey(Tenant, related_name="accounts", on_delete=models.CASCADE)


class Subscription(models.Model):
    account = models.ForeignKey(Account, related_name="subscriptions", on_delete=models.CASCADE)


class Charge(models.Model):
    subscription = models.ForeignKey(Subscription, related_name="charges", on_delete=models.CASCADE)
    amount = models.IntegerField()


# --- UniqueConstraintTrigger: index=True backing models (issue #10) ---


class IndexedSlugPage(models.Model):
    """Plain single-field unique index backing."""

    slug = models.SlugField(unique=False)  # uniqueness enforced by our trigger+index

    class Meta:
        triggers = [
            UniqueConstraintTrigger(
                fields=["slug"],
                index=True,
                name="indexedslugpage_slug_unique",
            ),
        ]


class IndexedCompositePage(models.Model):
    """Composite unique index backing (fields=['slug','section'])."""

    slug = models.SlugField()
    section = models.CharField(max_length=50, default="main")

    class Meta:
        triggers = [
            UniqueConstraintTrigger(
                fields=["slug", "section"],
                index=True,
                name="indexedcompositepage_slug_section_unique",
            ),
        ]


class IndexedLowerPage(models.Model):
    """Functional unique index backing (expressions=(Lower('slug'),))."""

    slug = models.SlugField()

    class Meta:
        triggers = [
            UniqueConstraintTrigger(
                Lower("slug"),
                index=True,
                name="indexedlowerpage_lower_slug_unique",
            ),
        ]


class IndexedNullsNotDistinctPage(models.Model):
    """NULLS NOT DISTINCT unique index backing (PG 15+)."""

    slug = models.SlugField(null=True)  # noqa: DJ001 — nullable for the test

    class Meta:
        triggers = [
            UniqueConstraintTrigger(
                fields=["slug"],
                index=True,
                nulls_distinct=False,
                name="indexednullsnotdistinctpage_slug_unique",
            ),
        ]


class IndexedPartialPage(models.Model):
    """Partial unique index backing (condition=Q(published=True))."""

    slug = models.SlugField()
    published = models.BooleanField(default=False)

    class Meta:
        triggers = [
            UniqueConstraintTrigger(
                fields=["slug"],
                condition=Q(published=True),
                index=True,
                name="indexedpartialpage_slug_published_unique",
            ),
        ]
