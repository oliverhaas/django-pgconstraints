"""Factory Boy factories for the testapp models.

Factories use SubFactory / LazyAttribute to wire up relationships automatically,
so tests only need to override the fields they actually care about.
"""

from decimal import Decimal

import factory
from factory.django import DjangoModelFactory
from testapp.models import (
    Chapter,
    LineItem,
    OrderLine,
    Page,
    Part,
    Product,
    PurchaseItem,
    Series,
    Supplier,
)

# ---------------------------------------------------------------------------
# UniqueConstraintTrigger models
# ---------------------------------------------------------------------------


class PageFactory(DjangoModelFactory):
    class Meta:
        model = Page

    slug = factory.Sequence(lambda n: f"page-{n}")
    section = "main"


class PublisherFactory(DjangoModelFactory):
    class Meta:
        model = "testapp.Publisher"

    name = factory.Sequence(lambda n: f"Publisher {n}")


class SeriesFactory(DjangoModelFactory):
    class Meta:
        model = Series

    title = factory.Sequence(lambda n: f"Series {n}")
    publisher = factory.SubFactory(PublisherFactory)


class ChapterFactory(DjangoModelFactory):
    class Meta:
        model = Chapter

    name = factory.Sequence(lambda n: f"Chapter {n}")
    series = factory.SubFactory(SeriesFactory)


# ---------------------------------------------------------------------------
# CheckConstraintTrigger models
# ---------------------------------------------------------------------------


class ProductFactory(DjangoModelFactory):
    class Meta:
        model = Product

    name = factory.Sequence(lambda n: f"Product {n}")
    stock = 10
    max_order_quantity = 100


class OrderLineFactory(DjangoModelFactory):
    class Meta:
        model = OrderLine

    product = factory.SubFactory(ProductFactory)
    quantity = 1


# ---------------------------------------------------------------------------
# GeneratedFieldTrigger models
# ---------------------------------------------------------------------------


class LineItemFactory(DjangoModelFactory):
    class Meta:
        model = LineItem

    description = factory.Sequence(lambda n: f"Item {n}")
    price = Decimal("10.00")
    quantity = 1


class SupplierFactory(DjangoModelFactory):
    class Meta:
        model = Supplier

    name = factory.Sequence(lambda n: f"Supplier {n}")
    markup_pct = 10


class PartFactory(DjangoModelFactory):
    class Meta:
        model = Part

    name = factory.Sequence(lambda n: f"Part {n}")
    supplier = factory.SubFactory(SupplierFactory)
    base_price = Decimal("10.00")


class PurchaseItemFactory(DjangoModelFactory):
    class Meta:
        model = PurchaseItem

    part = factory.SubFactory(PartFactory)
    quantity = 1
