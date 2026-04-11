"""Tests for GeneratedFieldTrigger — TDD, starting with same-row expressions."""

from decimal import Decimal

import pytest
from django.db.models import F
from testapp.models import LineItem, Part, PurchaseItem, Supplier

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


# ---------------------------------------------------------------------------
# Single-hop FK traversal: expression references a related model's field
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
class TestFKTraversalExpression:
    """Generated field computed from a related model's field via FK."""

    def test_insert_computes_from_related(self):
        """line_total = quantity * part.base_price, resolved via FK."""
        part = Part.objects.create(
            name="Bolt",
            supplier=Supplier.objects.create(name="Acme"),
            base_price=Decimal("2.50"),
        )
        item = PurchaseItem.objects.create(part=part, quantity=10)
        item.refresh_from_db()
        assert item.line_total == Decimal("25.00")

    def test_update_quantity_recomputes(self):
        part = Part.objects.create(
            name="Bolt",
            supplier=Supplier.objects.create(name="Acme"),
            base_price=Decimal("2.50"),
        )
        item = PurchaseItem.objects.create(part=part, quantity=10)
        item.quantity = 4
        item.save()
        item.refresh_from_db()
        assert item.line_total == Decimal("10.00")

    def test_reassign_fk_recomputes(self):
        """Changing the FK to a different part recomputes with the new price."""
        supplier = Supplier.objects.create(name="Acme")
        cheap = Part.objects.create(name="Cheap", supplier=supplier, base_price=Decimal("1.00"))
        expensive = Part.objects.create(name="Expensive", supplier=supplier, base_price=Decimal("50.00"))
        item = PurchaseItem.objects.create(part=cheap, quantity=3)
        item.refresh_from_db()
        assert item.line_total == Decimal("3.00")

        item.part = expensive
        item.save()
        item.refresh_from_db()
        assert item.line_total == Decimal("150.00")

    def test_related_field_change_auto_updates(self):
        """Changing part.base_price auto-updates all referencing PurchaseItems."""
        supplier = Supplier.objects.create(name="Acme")
        part = Part.objects.create(name="Bolt", supplier=supplier, base_price=Decimal("2.50"))
        item1 = PurchaseItem.objects.create(part=part, quantity=10)
        item2 = PurchaseItem.objects.create(part=part, quantity=4)

        part.base_price = Decimal("5.00")
        part.save()

        item1.refresh_from_db()
        item2.refresh_from_db()
        assert item1.line_total == Decimal("50.00")
        assert item2.line_total == Decimal("20.00")

    def test_related_field_change_only_affects_referencing_rows(self):
        """Changing one part's price doesn't affect items linked to other parts."""
        supplier = Supplier.objects.create(name="Acme")
        bolt = Part.objects.create(name="Bolt", supplier=supplier, base_price=Decimal("2.50"))
        nut = Part.objects.create(name="Nut", supplier=supplier, base_price=Decimal("0.50"))
        bolt_item = PurchaseItem.objects.create(part=bolt, quantity=10)
        nut_item = PurchaseItem.objects.create(part=nut, quantity=100)

        bolt.base_price = Decimal("10.00")
        bolt.save()

        bolt_item.refresh_from_db()
        nut_item.refresh_from_db()
        assert bolt_item.line_total == Decimal("100.00")
        assert nut_item.line_total == Decimal("50.00")  # unchanged

    def test_bulk_create(self):
        supplier = Supplier.objects.create(name="Acme")
        bolt = Part.objects.create(name="Bolt", supplier=supplier, base_price=Decimal("2.50"))
        nut = Part.objects.create(name="Nut", supplier=supplier, base_price=Decimal("0.50"))
        PurchaseItem.objects.bulk_create(
            [
                PurchaseItem(part=bolt, quantity=10),
                PurchaseItem(part=nut, quantity=100),
            ],
        )
        totals = PurchaseItem.objects.order_by("part__name").values_list("line_total", flat=True)
        assert list(totals) == [Decimal("25.00"), Decimal("50.00")]


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
