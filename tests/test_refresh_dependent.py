"""Tests for refresh_dependent(queryset) public API."""

from decimal import Decimal

import pytest
from django.db import connection
from factories import (
    PartFactory,
    PurchaseItemFactory,
    SupplierFactory,
)
from testapp.models import Page, Supplier

from django_pgconstraints import refresh_dependent
from django_pgconstraints.triggers import _build_chain_back_where

D = Decimal


@pytest.mark.unit
def test_build_chain_back_where_is_module_level():
    """The chain_back SQL builder must be importable as a module-level helper
    so refresh_dependent and _GeneratedFieldReverse can share it without
    instantiating a reverse trigger."""
    assert callable(_build_chain_back_where)


# ---------------------------------------------------------------------------
# Single-hop reconciliation — Supplier → Part.markup_amount
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_refresh_dependent_reconciles_single_hop_after_corruption():
    """refresh_dependent(Supplier.qs) must recompute Part.markup_amount for
    every Part that points at the queryset's Suppliers, even if triggers
    were bypassed earlier and the stored value is stale."""
    supplier = SupplierFactory.create(markup_pct=20)
    part = PartFactory.create(supplier=supplier, base_price=D("100.00"))
    part.refresh_from_db()
    assert part.markup_amount == D("20.00")

    # Simulate bypass: disable triggers, corrupt the field, re-enable.
    with connection.cursor() as cur:
        cur.execute("ALTER TABLE testapp_part DISABLE TRIGGER ALL")
        cur.execute(
            "UPDATE testapp_part SET markup_amount = 999 WHERE id = %s",
            [part.pk],
        )
        cur.execute("ALTER TABLE testapp_part ENABLE TRIGGER ALL")

    part.refresh_from_db()
    assert part.markup_amount == D("999.00")  # confirm corruption took

    refresh_dependent(Supplier.objects.filter(pk=supplier.pk))

    part.refresh_from_db()
    assert part.markup_amount == D("20.00"), "refresh_dependent did not reconcile the corrupted markup_amount"


@pytest.mark.django_db(transaction=True)
def test_refresh_dependent_only_touches_queryset_members():
    """Other suppliers' parts must not be touched."""
    s1 = SupplierFactory.create(markup_pct=10)
    s2 = SupplierFactory.create(markup_pct=50)
    p1 = PartFactory.create(supplier=s1, base_price=D("100.00"))
    p2 = PartFactory.create(supplier=s2, base_price=D("100.00"))

    with connection.cursor() as cur:
        cur.execute("ALTER TABLE testapp_part DISABLE TRIGGER ALL")
        cur.execute("UPDATE testapp_part SET markup_amount = 777")
        cur.execute("ALTER TABLE testapp_part ENABLE TRIGGER ALL")

    # Only refresh s1's parts.
    refresh_dependent(Supplier.objects.filter(pk=s1.pk))

    p1.refresh_from_db()
    p2.refresh_from_db()
    assert p1.markup_amount == D("10.00"), "p1 should have been reconciled"
    assert p2.markup_amount == D("777.00"), "p2 should NOT have been touched"


# ---------------------------------------------------------------------------
# Two-hop reconciliation — Supplier → PurchaseItem.supplier_markup (via Part)
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_refresh_dependent_reconciles_two_hop_after_corruption():
    supplier = SupplierFactory.create(markup_pct=15)
    part = PartFactory.create(supplier=supplier, base_price=D("10.00"))
    item = PurchaseItemFactory.create(part=part, quantity=5)
    item.refresh_from_db()
    assert item.supplier_markup == 15

    with connection.cursor() as cur:
        cur.execute("ALTER TABLE testapp_purchaseitem DISABLE TRIGGER ALL")
        cur.execute(
            "UPDATE testapp_purchaseitem SET supplier_markup = 0 WHERE id = %s",
            [item.pk],
        )
        cur.execute("ALTER TABLE testapp_purchaseitem ENABLE TRIGGER ALL")

    item.refresh_from_db()
    assert item.supplier_markup == 0

    refresh_dependent(Supplier.objects.filter(pk=supplier.pk))

    item.refresh_from_db()
    assert item.supplier_markup == 15


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_refresh_dependent_empty_queryset_is_noop():
    """An empty queryset must not raise and must not touch any rows."""
    SupplierFactory.create(markup_pct=10)
    # filter that matches nothing
    refresh_dependent(Supplier.objects.filter(pk=-999))
    # no assertion needed — the test passes if refresh_dependent didn't raise


@pytest.mark.django_db(transaction=True)
def test_refresh_dependent_on_model_with_no_dependents_is_noop():
    """Calling refresh_dependent with a queryset on a model that no
    GeneratedFieldTrigger references must not raise."""
    # Page has no GeneratedFieldTrigger; it is the Meta.triggers host for
    # a UniqueConstraintTrigger only;
    # nothing depends on Page in a GeneratedFieldTrigger FK chain.
    refresh_dependent(Page.objects.all())
    # test passes if this didn't raise
