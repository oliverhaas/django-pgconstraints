"""Tests for GeneratedFieldTrigger.

Covers: same-row expressions, FK-traversal expressions, single-hop and
two-hop reverse triggers (related-model changes auto-propagate), and
construction.
"""

from decimal import Decimal

import pytest
from django.db import connection
from django.db.models import F
from testapp.factories import (
    LineItemFactory,
    PartFactory,
    PurchaseItemFactory,
    SupplierFactory,
)
from testapp.models import LineItem, PurchaseItem

from django_pgconstraints import GeneratedFieldTrigger

D = Decimal


# ---------------------------------------------------------------------------
# Same-row expression (equivalent to Django's GeneratedField)
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_same_row_insert_computes_value():
    """total = price * quantity, computed on INSERT."""
    item = LineItemFactory.create(price=D("10.00"), quantity=3)
    item.refresh_from_db()
    assert item.total == D("30.00")


@pytest.mark.django_db(transaction=True)
def test_same_row_update_recomputes():
    item = LineItemFactory.create(price=D("10.00"), quantity=3)
    item.quantity = 5
    item.save()
    item.refresh_from_db()
    assert item.total == D("50.00")


@pytest.mark.django_db(transaction=True)
def test_same_row_manual_set_overridden_on_insert():
    item = LineItemFactory.create(price=D("10.00"), quantity=3, total=D("999.00"))
    item.refresh_from_db()
    assert item.total == D("30.00")


@pytest.mark.django_db(transaction=True)
def test_same_row_manual_set_overridden_on_update():
    item = LineItemFactory.create(price=D("10.00"), quantity=3)
    item.total = D("999.00")
    item.save()
    item.refresh_from_db()
    assert item.total == D("30.00")


@pytest.mark.django_db(transaction=True)
def test_same_row_raw_sql_update_recomputed():
    """Raw UPDATE that touches a source field triggers a recompute."""
    item = LineItemFactory.create(price=D("10.00"), quantity=3)
    with connection.cursor() as cur:
        cur.execute(
            'UPDATE testapp_lineitem SET "total" = 999, "quantity" = 5 WHERE id = %s',
            [item.pk],
        )
    item.refresh_from_db()
    assert item.total == D("50.00")


@pytest.mark.django_db(transaction=True)
def test_same_row_multiple_inserts():
    LineItemFactory.create(description="A", price=D("5.00"), quantity=2)
    LineItemFactory.create(description="B", price=D("7.50"), quantity=4)
    totals = LineItem.objects.order_by("description").values_list("total", flat=True)
    assert list(totals) == [D("10.00"), D("30.00")]


@pytest.mark.django_db(transaction=True)
def test_same_row_bulk_create():
    LineItem.objects.bulk_create(
        [
            LineItem(description="A", price=D("5.00"), quantity=2),
            LineItem(description="B", price=D("7.50"), quantity=4),
        ],
    )
    totals = LineItem.objects.order_by("description").values_list("total", flat=True)
    assert list(totals) == [D("10.00"), D("30.00")]


# ---------------------------------------------------------------------------
# Same-row string expression (Lower("description"))
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_same_row_string_insert_computes_slug():
    item = LineItemFactory.create(description="Hello World")
    item.refresh_from_db()
    assert item.slug == "hello world"


@pytest.mark.django_db(transaction=True)
def test_same_row_string_update_recomputes_slug():
    item = LineItemFactory.create(description="Hello")
    item.description = "Goodbye"
    item.save()
    item.refresh_from_db()
    assert item.slug == "goodbye"


# ---------------------------------------------------------------------------
# Single-hop FK traversal (PurchaseItem.line_total = quantity * part.base_price)
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_fk_insert_computes_from_related():
    part = PartFactory.create(base_price=D("2.50"))
    item = PurchaseItemFactory.create(part=part, quantity=10)
    item.refresh_from_db()
    assert item.line_total == D("25.00")


@pytest.mark.django_db(transaction=True)
def test_fk_update_quantity_recomputes():
    part = PartFactory.create(base_price=D("2.50"))
    item = PurchaseItemFactory.create(part=part, quantity=10)
    item.quantity = 4
    item.save()
    item.refresh_from_db()
    assert item.line_total == D("10.00")


@pytest.mark.django_db(transaction=True)
def test_fk_reassign_recomputes_with_new_related_value():
    supplier = SupplierFactory.create()
    cheap = PartFactory.create(supplier=supplier, base_price=D("1.00"))
    expensive = PartFactory.create(supplier=supplier, base_price=D("50.00"))
    item = PurchaseItemFactory.create(part=cheap, quantity=3)
    item.refresh_from_db()
    assert item.line_total == D("3.00")

    item.part = expensive
    item.save()
    item.refresh_from_db()
    assert item.line_total == D("150.00")


@pytest.mark.django_db(transaction=True)
def test_fk_related_field_change_propagates_to_all_referencers():
    """Changing part.base_price auto-updates all PurchaseItems pointing at it."""
    part = PartFactory.create(base_price=D("2.50"))
    item1 = PurchaseItemFactory.create(part=part, quantity=10)
    item2 = PurchaseItemFactory.create(part=part, quantity=4)

    part.base_price = D("5.00")
    part.save()

    item1.refresh_from_db()
    item2.refresh_from_db()
    assert item1.line_total == D("50.00")
    assert item2.line_total == D("20.00")


@pytest.mark.django_db(transaction=True)
def test_fk_related_field_change_only_affects_referencing_rows():
    supplier = SupplierFactory.create()
    bolt = PartFactory.create(supplier=supplier, base_price=D("2.50"))
    nut = PartFactory.create(supplier=supplier, base_price=D("0.50"))
    bolt_item = PurchaseItemFactory.create(part=bolt, quantity=10)
    nut_item = PurchaseItemFactory.create(part=nut, quantity=100)

    bolt.base_price = D("10.00")
    bolt.save()

    bolt_item.refresh_from_db()
    nut_item.refresh_from_db()
    assert bolt_item.line_total == D("100.00")
    assert nut_item.line_total == D("50.00")


@pytest.mark.django_db(transaction=True)
def test_fk_bulk_create():
    supplier = SupplierFactory.create()
    bolt = PartFactory.create(name="Bolt", supplier=supplier, base_price=D("2.50"))
    nut = PartFactory.create(name="Nut", supplier=supplier, base_price=D("0.50"))
    PurchaseItem.objects.bulk_create(
        [
            PurchaseItem(part=bolt, quantity=10),
            PurchaseItem(part=nut, quantity=100),
        ],
    )
    totals = PurchaseItem.objects.order_by("part__name").values_list("line_total", flat=True)
    assert list(totals) == [D("25.00"), D("50.00")]


# ---------------------------------------------------------------------------
# Single-hop reverse trigger (Part.markup_amount = base_price * supplier.markup_pct / 100)
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_single_hop_reverse_insert_computes():
    supplier = SupplierFactory.create(markup_pct=20)
    part = PartFactory.create(supplier=supplier, base_price=D("100.00"))
    part.refresh_from_db()
    assert part.markup_amount == D("20.00")


@pytest.mark.django_db(transaction=True)
def test_single_hop_reverse_supplier_markup_change_updates_parts():
    supplier = SupplierFactory.create(markup_pct=10)
    p1 = PartFactory.create(supplier=supplier, base_price=D("100.00"))
    p2 = PartFactory.create(supplier=supplier, base_price=D("50.00"))

    supplier.markup_pct = 25
    supplier.save()

    p1.refresh_from_db()
    p2.refresh_from_db()
    assert p1.markup_amount == D("25.00")
    assert p2.markup_amount == D("12.50")


@pytest.mark.django_db(transaction=True)
def test_single_hop_reverse_only_affects_own_parts():
    s1 = SupplierFactory.create(markup_pct=10)
    s2 = SupplierFactory.create(markup_pct=20)
    p1 = PartFactory.create(supplier=s1, base_price=D("100.00"))
    p2 = PartFactory.create(supplier=s2, base_price=D("100.00"))

    s1.markup_pct = 50
    s1.save()

    p1.refresh_from_db()
    p2.refresh_from_db()
    assert p1.markup_amount == D("50.00")
    assert p2.markup_amount == D("20.00")


# ---------------------------------------------------------------------------
# Two-hop reverse trigger (PurchaseItem.supplier_markup = part.supplier.markup_pct)
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_two_hop_reverse_insert_resolves():
    supplier = SupplierFactory.create(markup_pct=15)
    part = PartFactory.create(supplier=supplier, base_price=D("10.00"))
    item = PurchaseItemFactory.create(part=part, quantity=1)
    item.refresh_from_db()
    assert item.supplier_markup == 15


@pytest.mark.django_db(transaction=True)
def test_two_hop_reverse_supplier_change_propagates_through_part():
    supplier = SupplierFactory.create(markup_pct=10)
    part = PartFactory.create(supplier=supplier, base_price=D("10.00"))
    item = PurchaseItemFactory.create(part=part, quantity=5)
    item.refresh_from_db()
    assert item.supplier_markup == 10

    supplier.markup_pct = 30
    supplier.save()

    item.refresh_from_db()
    assert item.supplier_markup == 30


@pytest.mark.django_db(transaction=True)
def test_two_hop_reverse_part_supplier_reassign_updates_items():
    s1 = SupplierFactory.create(markup_pct=10)
    s2 = SupplierFactory.create(markup_pct=50)
    part = PartFactory.create(supplier=s1, base_price=D("10.00"))
    item = PurchaseItemFactory.create(part=part, quantity=5)
    item.refresh_from_db()
    assert item.supplier_markup == 10

    part.supplier = s2
    part.save()

    item.refresh_from_db()
    assert item.supplier_markup == 50


@pytest.mark.django_db(transaction=True)
def test_two_hop_reverse_only_affected_items_updated():
    s1 = SupplierFactory.create(markup_pct=10)
    s2 = SupplierFactory.create(markup_pct=20)
    p1 = PartFactory.create(supplier=s1, base_price=D("10.00"))
    p2 = PartFactory.create(supplier=s2, base_price=D("10.00"))
    item1 = PurchaseItemFactory.create(part=p1, quantity=1)
    item2 = PurchaseItemFactory.create(part=p2, quantity=1)

    s1.markup_pct = 99
    s1.save()

    item1.refresh_from_db()
    item2.refresh_from_db()
    assert item1.supplier_markup == 99
    assert item2.supplier_markup == 20


# ---------------------------------------------------------------------------
# Construction (pure Python, no DB)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_construction_basic():
    t = GeneratedFieldTrigger(
        field="total",
        expression=F("price") * F("quantity"),
        name="c",
    )
    assert t.field == "total"


@pytest.mark.unit
def test_construction_field_required():
    with pytest.raises(TypeError):
        GeneratedFieldTrigger(expression=F("x"), name="c")  # type: ignore[call-arg]


@pytest.mark.unit
def test_construction_expression_required():
    with pytest.raises(TypeError):
        GeneratedFieldTrigger(field="total", name="c")  # type: ignore[call-arg]
