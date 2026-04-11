"""Phase 2 tests: statement-level reverse triggers coalesce bulk cascades.

These tests verify that a single UPDATE statement touching N parent rows
cascades to all N sets of children correctly, and that the deployed
triggers are actually statement-level (not row-level, not misconfigured).
"""

from decimal import Decimal

import pytest
from django.db import connection
from factories import (
    PartFactory,
    PurchaseItemFactory,
    SupplierFactory,
)
from testapp.models import Part, PurchaseItem, Supplier

from django_pgconstraints import GeneratedFieldTrigger

D = Decimal


# ---------------------------------------------------------------------------
# Bulk UPDATE on parent cascades to all children in one statement
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_bulk_update_many_suppliers_cascades_to_all_parts():
    """A single UPDATE on several suppliers must cascade to every part."""
    s1 = SupplierFactory.create(markup_pct=10)
    s2 = SupplierFactory.create(markup_pct=10)
    s3 = SupplierFactory.create(markup_pct=10)
    p1 = PartFactory.create(supplier=s1, base_price=D("100.00"))
    p2 = PartFactory.create(supplier=s2, base_price=D("100.00"))
    p3 = PartFactory.create(supplier=s3, base_price=D("100.00"))

    # Bulk ORM update: single UPDATE statement touching all 3 suppliers.
    Supplier.objects.filter(pk__in=[s1.pk, s2.pk, s3.pk]).update(markup_pct=50)

    p1.refresh_from_db()
    p2.refresh_from_db()
    p3.refresh_from_db()
    assert p1.markup_amount == D("50.00")
    assert p2.markup_amount == D("50.00")
    assert p3.markup_amount == D("50.00")


@pytest.mark.django_db(transaction=True)
def test_bulk_update_only_changed_rows_cascade():
    """Bulk UPDATE where only some rows actually change value.

    The IS DISTINCT FROM gate lives in the WHERE clause now. Suppliers
    whose markup_pct happens to be set to its existing value must NOT
    trigger a recompute of their parts (even though those rows appear
    in new_rows/old_rows for the statement). Verified via the
    pg_stat_xact_user_tables counter on testapp_part.
    """
    s1 = SupplierFactory.create(markup_pct=10)
    s2 = SupplierFactory.create(markup_pct=99)  # will be re-set to 99 -> no-op
    # Precondition: s2's markup_pct already equals the no-op target so that
    # the CASE clause below is genuinely a no-op for s2 / p2.
    assert s2.markup_pct == 99
    p1 = PartFactory.create(supplier=s1, base_price=D("100.00"))
    p2 = PartFactory.create(supplier=s2, base_price=D("100.00"))
    p1.refresh_from_db()
    p2.refresh_from_db()
    initial_p2 = p2.markup_amount

    with connection.cursor() as cur:
        cur.execute(
            "SELECT n_tup_upd FROM pg_stat_xact_user_tables WHERE relname = 'testapp_part'",
        )
        row = cur.fetchone()
        updates_before = row[0] if row else 0

    # Bulk UPDATE: s1 gets a real change, s2 gets set to its existing value.
    # Both rows appear in new_rows/old_rows; only s1 should drive a cascade.
    with connection.cursor() as cur:
        cur.execute(
            "UPDATE testapp_supplier SET markup_pct = CASE id WHEN %s THEN 25 WHEN %s THEN 99 END WHERE id IN (%s, %s)",
            [s1.pk, s2.pk, s1.pk, s2.pk],
        )

    p1.refresh_from_db()
    p2.refresh_from_db()
    # s1 changed -> p1 recomputed
    assert p1.markup_amount == D("25.00")
    # s2 no-op -> p2 unchanged
    assert p2.markup_amount == initial_p2

    with connection.cursor() as cur:
        cur.execute(
            "SELECT n_tup_upd FROM pg_stat_xact_user_tables WHERE relname = 'testapp_part'",
        )
        row = cur.fetchone()
        updates_after = row[0] if row else 0

    # Exactly one part row was updated (p1). p2's cascade was skipped by
    # the WHERE-clause IS DISTINCT FROM gate.
    assert updates_after - updates_before == 1, (
        f"Expected exactly one cascaded part update (p1 only), "
        f"got {updates_after - updates_before}. "
        "The IS DISTINCT FROM gate in the WHERE clause is not filtering no-op rows."
    )


@pytest.mark.django_db(transaction=True)
def test_bulk_update_two_hop_cascades_to_all_purchase_items():
    """Bulk supplier update cascades through Part to PurchaseItem in one shot."""
    s1 = SupplierFactory.create(markup_pct=10)
    s2 = SupplierFactory.create(markup_pct=10)
    part1 = PartFactory.create(supplier=s1, base_price=D("10.00"))
    part2 = PartFactory.create(supplier=s2, base_price=D("10.00"))
    item1 = PurchaseItemFactory.create(part=part1, quantity=1)
    item2 = PurchaseItemFactory.create(part=part2, quantity=1)

    Supplier.objects.filter(pk__in=[s1.pk, s2.pk]).update(markup_pct=77)

    item1.refresh_from_db()
    item2.refresh_from_db()
    assert item1.supplier_markup == 77
    assert item2.supplier_markup == 77


@pytest.mark.django_db(transaction=True)
def test_bulk_part_reassignment_cascades_to_purchase_items():
    """Bulk Part.supplier_id reassignment (intermediate FK column) cascades."""
    s1 = SupplierFactory.create(markup_pct=10)
    s2 = SupplierFactory.create(markup_pct=40)
    p1 = PartFactory.create(supplier=s1, base_price=D("10.00"))
    p2 = PartFactory.create(supplier=s1, base_price=D("10.00"))
    item1 = PurchaseItemFactory.create(part=p1, quantity=1)
    item2 = PurchaseItemFactory.create(part=p2, quantity=1)
    item1.refresh_from_db()
    item2.refresh_from_db()
    assert item1.supplier_markup == 10
    assert item2.supplier_markup == 10

    # Bulk reassign both parts to s2 in one UPDATE statement.
    Part.objects.filter(pk__in=[p1.pk, p2.pk]).update(supplier=s2)

    item1.refresh_from_db()
    item2.refresh_from_db()
    assert item1.supplier_markup == 40
    assert item2.supplier_markup == 40


# ---------------------------------------------------------------------------
# Deployed trigger introspection -- verify pg_trigger says STATEMENT-level
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_deployed_reverse_trigger_is_statement_level():
    """pg_trigger.tgtype bit 0 is set for row-level triggers. Verify every
    reverse trigger installed in the actual database is statement-level.

    Reverse triggers are emitted with a ``_rev_<fk_name>`` suffix in their
    name (see ``GeneratedFieldTrigger.get_reverse_triggers``), which is the
    simplest reliable way to discriminate them from the row-level forward
    triggers that compute the generated field on the owning table.

    tgtype bit layout (from src/include/catalog/pg_trigger.h):
        bit 0: TRIGGER_TYPE_ROW (1 = row-level, 0 = statement-level)
    """
    # Collect every reverse trigger Python says should be installed, by
    # asking GeneratedFieldTrigger.get_reverse_triggers itself. Deriving
    # the floor from the source of truth means a silent regression that
    # drops a reverse-trigger site fails this test instead of vacuously
    # passing on whatever survived.
    expected_reverse_triggers: list = []
    for model in (Part, PurchaseItem):
        for trig in model._meta.triggers:
            if isinstance(trig, GeneratedFieldTrigger):
                expected_reverse_triggers.extend(trig.get_reverse_triggers(model))

    assert expected_reverse_triggers, (
        "test schema must declare at least one reverse trigger; "
        "if this fails the fixture models lost their GeneratedFieldTrigger FKs"
    )

    with connection.cursor() as cur:
        cur.execute(
            "SELECT c.relname AS tablename, t.tgname, t.tgtype & 1 AS is_row "
            "FROM pg_trigger t "
            "JOIN pg_class c ON c.oid = t.tgrelid "
            "WHERE t.tgname LIKE 'pgtrigger_%%_rev_%%' "
            "AND NOT t.tgisinternal",
        )
        rows = cur.fetchall()

    assert len(rows) == len(expected_reverse_triggers), (
        f"expected {len(expected_reverse_triggers)} reverse triggers installed "
        f"in pg_trigger, got {len(rows)}: {[r[1] for r in rows]}. "
        "Either get_reverse_triggers silently dropped a site or the _rev_ "
        "naming convention changed."
    )

    for tablename, tgname, is_row in rows:
        assert is_row == 0, (
            f"trigger {tgname} on {tablename} is row-level (tgtype & 1 == 1); "
            "reverse triggers must be statement-level after Phase 2"
        )
