from django.db import models
from django.db.models import F, Q

from django_pgconstraints import (
    AllowedTransitions,
    CheckConstraintTrigger,
    Immutable,
    MaintainedCount,
    UniqueConstraintTrigger,
)

# --- UniqueConstraintTrigger models ---


class Page(models.Model):
    slug = models.SlugField(unique=True)

    class Meta:
        triggers = [
            UniqueConstraintTrigger(
                field="slug",
                across="testapp.Post",
                name="page_unique_slug_across_post",
            ),
        ]


class Post(models.Model):
    slug = models.SlugField(unique=True)

    class Meta:
        triggers = [
            UniqueConstraintTrigger(
                field="slug",
                across="testapp.Page",
                name="post_unique_slug_across_page",
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
