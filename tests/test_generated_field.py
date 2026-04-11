"""Tests for GeneratedFieldTrigger — TDD, starting with same-row expressions."""

from decimal import Decimal

import pytest
from django.db.models import F
from testapp.models import LineItem

from django_pgconstraints import GeneratedFieldTrigger

# ---------------------------------------------------------------------------
# Same-row expression (equivalent to Django's GeneratedField)
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
class TestSameRowExpression:
    """Generated field computed from local fields only."""

    def test_insert_computes_value(self):
        """total = price * quantity, computed on INSERT."""
        item = LineItem.objects.create(description="Widget", price=Decimal("10.00"), quantity=3)
        item.refresh_from_db()
        assert item.total == Decimal("30.00")

    def test_update_recomputes(self):
        """Changing a source field recomputes the generated field."""
        item = LineItem.objects.create(description="Widget", price=Decimal("10.00"), quantity=3)
        item.quantity = 5
        item.save()
        item.refresh_from_db()
        assert item.total == Decimal("50.00")

    def test_manual_set_overridden(self):
        """Even if you manually set the field, the trigger overwrites it."""
        item = LineItem.objects.create(
            description="Widget",
            price=Decimal("10.00"),
            quantity=3,
            total=Decimal("999.00"),
        )
        item.refresh_from_db()
        assert item.total == Decimal("30.00")

    def test_multiple_inserts(self):
        LineItem.objects.create(description="A", price=Decimal("5.00"), quantity=2)
        LineItem.objects.create(description="B", price=Decimal("7.50"), quantity=4)
        items = LineItem.objects.order_by("description").values_list("total", flat=True)
        assert list(items) == [Decimal("10.00"), Decimal("30.00")]

    def test_bulk_create(self):
        LineItem.objects.bulk_create(
            [
                LineItem(description="A", price=Decimal("5.00"), quantity=2),
                LineItem(description="B", price=Decimal("7.50"), quantity=4),
            ],
        )
        items = LineItem.objects.order_by("description").values_list("total", flat=True)
        assert list(items) == [Decimal("10.00"), Decimal("30.00")]


@pytest.mark.django_db(transaction=True)
class TestSameRowStringExpression:
    """Generated field with string concatenation expression."""

    def test_insert_computes_slug(self):
        item = LineItem.objects.create(description="Hello World", price=Decimal("1.00"), quantity=1)
        item.refresh_from_db()
        assert item.slug == "hello world"

    def test_update_recomputes_slug(self):
        item = LineItem.objects.create(description="Hello", price=Decimal("1.00"), quantity=1)
        item.description = "Goodbye"
        item.save()
        item.refresh_from_db()
        assert item.slug == "goodbye"


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestGeneratedFieldTriggerConstruction:
    def test_basic(self):
        t = GeneratedFieldTrigger(
            field="total",
            expression=F("price") * F("quantity"),
            name="c",
        )
        assert t.field == "total"

    def test_field_required(self):
        with pytest.raises(TypeError):
            GeneratedFieldTrigger(expression=F("x"), name="c")  # type: ignore[call-arg]

    def test_expression_required(self):
        with pytest.raises(TypeError):
            GeneratedFieldTrigger(field="total", name="c")  # type: ignore[call-arg]
