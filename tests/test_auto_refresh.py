"""Tests for RETURNING-based auto-refresh of GeneratedFieldTrigger targets.

Covers: save() INSERT, save() UPDATE, bulk_create, FK-traversal expressions,
and the per-trigger ``auto_refresh=False`` opt-out.
"""

from decimal import Decimal

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from factories import LineItemFactory, PartFactory, PurchaseItemFactory
from testapp.models import LineItem, ManualRefreshItem, PurchaseItem

D = Decimal


# ---------------------------------------------------------------------------
# save() INSERT and UPDATE — same-row expression (LineItem.total)
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_save_insert_populates_instance_without_refresh():
    item = LineItem(description="A", price=D("10.00"), quantity=3)
    item.save()
    # No refresh_from_db(): value arrives via RETURNING.
    assert item.total == D("30.00")


@pytest.mark.django_db(transaction=True)
def test_save_update_populates_instance_without_refresh():
    item = LineItemFactory.create(price=D("10.00"), quantity=3)
    item.quantity = 5
    item.save()
    assert item.total == D("50.00")


@pytest.mark.django_db(transaction=True)
def test_save_update_single_round_trip():
    """save() on UPDATE must not issue a separate SELECT for the refresh."""
    item = LineItemFactory.create(price=D("10.00"), quantity=3)
    item.quantity = 5
    with CaptureQueriesContext(connection) as ctx:
        item.save()
    assert len(ctx.captured_queries) == 1
    assert "RETURNING" in ctx.captured_queries[0]["sql"].upper()


@pytest.mark.django_db(transaction=True)
def test_save_insert_single_round_trip():
    """save() on INSERT must not issue a separate SELECT for the refresh."""
    with CaptureQueriesContext(connection) as ctx:
        LineItem(description="X", price=D("10.00"), quantity=3).save()
    # One INSERT, no trailing SELECT.
    assert len(ctx.captured_queries) == 1
    assert "RETURNING" in ctx.captured_queries[0]["sql"].upper()


@pytest.mark.django_db(transaction=True)
def test_save_populates_all_computed_fields():
    """A model with multiple GeneratedFieldTrigger entries refreshes them all."""
    item = LineItem(description="Hello World", price=D("2.00"), quantity=4)
    item.save()
    assert item.total == D("8.00")
    assert item.slug == "hello world"


# ---------------------------------------------------------------------------
# bulk_create — same-row
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_bulk_create_populates_instances():
    items = [
        LineItem(description="A", price=D("5.00"), quantity=2),
        LineItem(description="B", price=D("7.50"), quantity=4),
    ]
    created = LineItem.objects.bulk_create(items)
    assert created[0].total == D("10.00")
    assert created[1].total == D("30.00")


# ---------------------------------------------------------------------------
# bulk_create(update_conflicts=True) — the recommended bulk_update workaround
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_bulk_create_update_conflicts_refreshes_existing_rows():
    """UPSERT path: conflicting PKs hit DO UPDATE and the trigger recomputes."""
    existing = LineItemFactory.create(price=D("10.00"), quantity=3)
    assert existing.total == D("30.00")

    staged = LineItem(
        pk=existing.pk,
        description=existing.description,
        price=D("20.00"),
        quantity=5,
    )
    LineItem.objects.bulk_create(
        [staged],
        update_conflicts=True,
        update_fields=["price", "quantity"],
        unique_fields=["id"],
    )
    assert staged.total == D("100.00")


@pytest.mark.django_db(transaction=True)
def test_bulk_create_update_conflicts_refreshes_fresh_rows():
    """Mixed batch: rows without an existing PK go through the INSERT branch."""
    fresh = LineItem(description="fresh", price=D("3.00"), quantity=7)
    LineItem.objects.bulk_create(
        [fresh],
        update_conflicts=True,
        update_fields=["price", "quantity"],
        unique_fields=["id"],
    )
    assert fresh.pk is not None
    assert fresh.total == D("21.00")


# ---------------------------------------------------------------------------
# FK-traversal expression (PurchaseItem.line_total = quantity * part.base_price)
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_fk_save_insert_populates_instance():
    part = PartFactory.create(base_price=D("2.50"))
    item = PurchaseItem(part=part, quantity=10)
    item.save()
    assert item.line_total == D("25.00")


@pytest.mark.django_db(transaction=True)
def test_fk_save_update_populates_instance():
    part = PartFactory.create(base_price=D("2.50"))
    item = PurchaseItemFactory.create(part=part, quantity=10)
    item.quantity = 4
    item.save()
    assert item.line_total == D("10.00")


# ---------------------------------------------------------------------------
# auto_refresh=False opt-out
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_opt_out_insert_does_not_refresh_instance():
    item = ManualRefreshItem(price=D("10.00"), quantity=3)
    item.save()
    # Instance still holds the pre-trigger default; DB has the computed value.
    assert item.total == D("0.00")
    item.refresh_from_db()
    assert item.total == D("30.00")


@pytest.mark.django_db(transaction=True)
def test_opt_out_update_does_not_refresh_instance():
    item = ManualRefreshItem.objects.create(price=D("10.00"), quantity=3)
    item.refresh_from_db()  # start from the DB-computed value
    assert item.total == D("30.00")

    item.quantity = 5
    item.save()
    # Instance still holds the pre-update value; DB has the newly computed one.
    assert item.total == D("30.00")
    item.refresh_from_db()
    assert item.total == D("50.00")
