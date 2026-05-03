"""GeneratedFieldTrigger over a reverse relation aggregate (Sum, Count, ...).

The goal: ``GeneratedFieldTrigger(expression=Sum("lines__amount"))`` keeps
``Invoice.total`` in sync as ``InvoiceLine`` rows are inserted, deleted,
updated, or moved between invoices.

Each test in this module pins down one slice of that contract. They land
red and turn green as the implementation grows.
"""

import pytest
from django.db.models import Avg, Count, Max, Min, Sum
from testapp.models import (
    Account,
    Cart,
    CartItem,
    Charge,
    Customer,
    Invoice,
    InvoiceLine,
    Subscription,
    Tenant,
)

from django_pgconstraints import GeneratedFieldTrigger
from django_pgconstraints.cycles import CycleError, check_for_cycles
from django_pgconstraints.sql import _resolve_reverse_aggregate


@pytest.mark.django_db
def test_invoice_total_sums_lines_on_insert():
    invoice = Invoice.objects.create()
    InvoiceLine.objects.create(invoice=invoice, amount=10)
    InvoiceLine.objects.create(invoice=invoice, amount=15)

    invoice.refresh_from_db()
    assert invoice.total == 25


@pytest.mark.django_db
def test_invoice_total_recomputes_on_parent_update():
    """Forward trigger fires on parent UPDATE: total is recomputed from current children.

    This exercises only the parent-side compilation; until the reverse
    trigger lands, child writes alone don't refresh the parent total.
    """
    invoice = Invoice.objects.create()
    InvoiceLine.objects.create(invoice=invoice, amount=10)
    InvoiceLine.objects.create(invoice=invoice, amount=15)

    invoice.save()  # parent BEFORE UPDATE fires, recomputing total
    invoice.refresh_from_db()
    assert invoice.total == 25


@pytest.mark.django_db
def test_invoice_total_zero_for_empty_invoice():
    """COALESCE wraps the aggregate so empty children → 0, not NULL."""
    invoice = Invoice.objects.create()
    invoice.refresh_from_db()
    assert invoice.total == 0


@pytest.mark.django_db
def test_invoice_total_bulk_insert_lines():
    """One bulk_create touches the parent once via the transition table."""
    invoice = Invoice.objects.create()
    InvoiceLine.objects.bulk_create(
        [
            InvoiceLine(invoice=invoice, amount=5),
            InvoiceLine(invoice=invoice, amount=7),
            InvoiceLine(invoice=invoice, amount=11),
        ]
    )

    invoice.refresh_from_db()
    assert invoice.total == 23


@pytest.mark.django_db
def test_invoice_totals_isolated_across_invoices():
    """Inserting a line on invoice A must not touch invoice B's total."""
    a = Invoice.objects.create()
    b = Invoice.objects.create()
    InvoiceLine.objects.create(invoice=a, amount=3)
    InvoiceLine.objects.create(invoice=b, amount=100)
    InvoiceLine.objects.create(invoice=a, amount=4)

    a.refresh_from_db()
    b.refresh_from_db()
    assert a.total == 7
    assert b.total == 100


@pytest.mark.django_db
def test_invoice_total_recomputes_on_line_delete():
    invoice = Invoice.objects.create()
    a = InvoiceLine.objects.create(invoice=invoice, amount=10)
    InvoiceLine.objects.create(invoice=invoice, amount=15)
    invoice.refresh_from_db()
    assert invoice.total == 25

    a.delete()
    invoice.refresh_from_db()
    assert invoice.total == 15


@pytest.mark.django_db
def test_invoice_total_zero_when_all_lines_deleted():
    invoice = Invoice.objects.create()
    InvoiceLine.objects.create(invoice=invoice, amount=10)
    InvoiceLine.objects.create(invoice=invoice, amount=15)
    InvoiceLine.objects.filter(invoice=invoice).delete()

    invoice.refresh_from_db()
    assert invoice.total == 0


@pytest.mark.django_db
def test_cascade_delete_of_parent_does_not_error():
    """Deleting the parent cascades to children; the AFTER DELETE trigger
    fires but the parent is gone, so the UPDATE is a no-op."""
    invoice = Invoice.objects.create()
    InvoiceLine.objects.create(invoice=invoice, amount=10)
    invoice.delete()

    assert not Invoice.objects.exists()
    assert not InvoiceLine.objects.exists()


@pytest.mark.django_db
def test_invoice_total_recomputes_on_line_amount_update():
    invoice = Invoice.objects.create()
    line = InvoiceLine.objects.create(invoice=invoice, amount=10)
    InvoiceLine.objects.create(invoice=invoice, amount=15)
    invoice.refresh_from_db()
    assert invoice.total == 25

    line.amount = 100
    line.save()

    invoice.refresh_from_db()
    assert invoice.total == 115


@pytest.mark.django_db
def test_invoice_total_recomputes_on_bulk_update():
    invoice = Invoice.objects.create()
    InvoiceLine.objects.create(invoice=invoice, amount=10)
    InvoiceLine.objects.create(invoice=invoice, amount=15)

    InvoiceLine.objects.filter(invoice=invoice).update(amount=7)

    invoice.refresh_from_db()
    assert invoice.total == 14


@pytest.mark.django_db
def test_invoice_total_recomputes_when_line_pivots_to_other_invoice():
    """Moving a line from invoice A to invoice B: both totals updated."""
    a = Invoice.objects.create()
    b = Invoice.objects.create()
    line = InvoiceLine.objects.create(invoice=a, amount=42)
    InvoiceLine.objects.create(invoice=b, amount=8)

    a.refresh_from_db()
    b.refresh_from_db()
    assert a.total == 42
    assert b.total == 8

    line.invoice = b
    line.save()

    a.refresh_from_db()
    b.refresh_from_db()
    assert a.total == 0
    assert b.total == 50


# ---------------------------------------------------------------------------
# Compilation unit tests — every aggregate kind we claim to support
# ---------------------------------------------------------------------------


def _q(name: str) -> str:
    return f'"{name}"'


@pytest.mark.unit
@pytest.mark.parametrize(
    ("aggregate", "expected_sql"),
    [
        (
            Sum("lines__amount"),
            'COALESCE((SELECT SUM("amount") FROM "testapp_invoiceline" WHERE "invoice_id" = NEW."id"), 0)',
        ),
        (Count("lines"), 'COALESCE((SELECT COUNT(*) FROM "testapp_invoiceline" WHERE "invoice_id" = NEW."id"), 0)'),
        (
            Count("lines__amount"),
            'COALESCE((SELECT COUNT("amount") FROM "testapp_invoiceline" WHERE "invoice_id" = NEW."id"), 0)',
        ),
        (Avg("lines__amount"), '(SELECT AVG("amount") FROM "testapp_invoiceline" WHERE "invoice_id" = NEW."id")'),
        (Max("lines__amount"), '(SELECT MAX("amount") FROM "testapp_invoiceline" WHERE "invoice_id" = NEW."id")'),
        (Min("lines__amount"), '(SELECT MIN("amount") FROM "testapp_invoiceline" WHERE "invoice_id" = NEW."id")'),
    ],
)
def test_resolve_reverse_aggregate_emits_expected_sql(aggregate, expected_sql):
    sql = _resolve_reverse_aggregate(aggregate, Invoice, _q, row_ref="NEW")
    assert sql == expected_sql


@pytest.mark.unit
def test_filter_aggregate_rejected_at_compile_time():
    from django.db.models import Q  # noqa: PLC0415

    with pytest.raises(NotImplementedError, match="filter"):
        _resolve_reverse_aggregate(
            Sum("lines__amount", filter=Q(amount__gt=0)),
            Invoice,
            _q,
            row_ref="NEW",
        )


@pytest.mark.unit
def test_distinct_aggregate_rejected_at_compile_time():
    with pytest.raises(NotImplementedError, match="distinct"):
        _resolve_reverse_aggregate(
            Count("lines__amount", distinct=True),
            Invoice,
            _q,
            row_ref="NEW",
        )


@pytest.mark.unit
def test_multi_hop_reverse_aggregate_compiles_to_nested_select():
    """Every hop reverse-FK: build nested SELECTs through intermediates."""
    sql = _resolve_reverse_aggregate(Sum("carts__items__amount"), Customer, _q, row_ref="NEW")
    assert sql == (
        'COALESCE((SELECT SUM("amount") FROM "testapp_cartitem" '
        'WHERE "cart_id" IN ('
        'SELECT "id" FROM "testapp_cart" WHERE "customer_id" = NEW."id"'
        ")), 0)"
    )


@pytest.mark.unit
def test_mixed_forward_reverse_aggregate_rejected():
    """Mixed reverse + forward FK chains aren't supported.

    On Invoice, ``lines`` is a reverse FK to InvoiceLine, but ``invoice`` on
    InvoiceLine is a forward FK back. Walking through it would land at a
    non-many relation, which we don't know how to denormalise yet.
    """
    with pytest.raises(ValueError, match="reverse one-to-many"):
        _resolve_reverse_aggregate(
            Sum("lines__invoice__total"),
            Invoice,
            _q,
            row_ref="NEW",
        )


@pytest.mark.unit
def test_forward_relation_in_aggregate_rejected():
    """Aggregates rooted at a forward FK aren't meaningful here either."""
    with pytest.raises(ValueError, match="reverse one-to-many"):
        _resolve_reverse_aggregate(
            Sum("invoice__total"),  # invoice is a forward FK on InvoiceLine
            InvoiceLine,
            _q,
            row_ref="NEW",
        )


# ---------------------------------------------------------------------------
# Cycle detection — a synthetic graph with an aggregate edge
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# UPDATE gating — irrelevant child UPDATEs must not recompute the parent
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_update_gating_skips_recompute_when_only_irrelevant_column_changed():
    """Updating a non-aggregated column on a child must not re-fire the
    parent's aggregate recompute.

    We prove this by sabotaging the parent under disabled triggers, then
    issuing an UPDATE on the child that only touches a non-watched column.
    If gating works, the sabotage value survives because the trigger
    short-circuits at the IS DISTINCT FROM filter.
    """
    from django.db import connection  # noqa: PLC0415

    invoice = Invoice.objects.create()
    line = InvoiceLine.objects.create(invoice=invoice, amount=10, note="initial")
    invoice.refresh_from_db()
    assert invoice.total == 10

    with connection.cursor() as cur:
        cur.execute("ALTER TABLE testapp_invoice DISABLE TRIGGER ALL")
        cur.execute("UPDATE testapp_invoice SET total = 999")
        cur.execute("ALTER TABLE testapp_invoice ENABLE TRIGGER ALL")

    line.note = "edited"
    line.save()

    invoice.refresh_from_db()
    assert invoice.total == 999, "gating should have prevented the parent recompute"


@pytest.mark.django_db(transaction=True)
def test_update_gating_fires_recompute_when_aggregated_column_changes():
    """Counterpart: a real change to the aggregated column re-fires the
    recompute, overwriting the sabotaged value."""
    from django.db import connection  # noqa: PLC0415

    invoice = Invoice.objects.create()
    line = InvoiceLine.objects.create(invoice=invoice, amount=10, note="initial")

    with connection.cursor() as cur:
        cur.execute("ALTER TABLE testapp_invoice DISABLE TRIGGER ALL")
        cur.execute("UPDATE testapp_invoice SET total = 999")
        cur.execute("ALTER TABLE testapp_invoice ENABLE TRIGGER ALL")

    line.amount = 50
    line.save()

    invoice.refresh_from_db()
    assert invoice.total == 50, "amount change should have fired recompute"


# ---------------------------------------------------------------------------
# refresh_dependent — reconcile aggregate parents after a trigger bypass
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_refresh_dependent_reconciles_aggregate_after_bypass():
    """Disable triggers, change line amounts directly, then call
    refresh_dependent on the lines queryset and verify Invoice.total
    is recomputed for every parent of those lines."""
    from django.db import connection  # noqa: PLC0415

    from django_pgconstraints import refresh_dependent  # noqa: PLC0415

    invoice = Invoice.objects.create()
    InvoiceLine.objects.create(invoice=invoice, amount=10)
    InvoiceLine.objects.create(invoice=invoice, amount=15)
    invoice.refresh_from_db()
    assert invoice.total == 25

    # Bypass: disable triggers, mutate line amounts in raw SQL, re-enable.
    with connection.cursor() as cur:
        cur.execute("ALTER TABLE testapp_invoiceline DISABLE TRIGGER ALL")
        cur.execute(
            "UPDATE testapp_invoiceline SET amount = 999 WHERE invoice_id = %s",
            [invoice.pk],
        )
        cur.execute("ALTER TABLE testapp_invoiceline ENABLE TRIGGER ALL")

    invoice.refresh_from_db()
    assert invoice.total == 25  # parent stale because triggers were bypassed

    refresh_dependent(InvoiceLine.objects.filter(invoice=invoice))

    invoice.refresh_from_db()
    assert invoice.total == 1998


@pytest.mark.django_db(transaction=True)
def test_refresh_dependent_aggregate_only_touches_queryset_invoices():
    from django.db import connection  # noqa: PLC0415

    from django_pgconstraints import refresh_dependent  # noqa: PLC0415

    a = Invoice.objects.create()
    b = Invoice.objects.create()
    InvoiceLine.objects.create(invoice=a, amount=10)
    InvoiceLine.objects.create(invoice=b, amount=20)

    with connection.cursor() as cur:
        cur.execute("ALTER TABLE testapp_invoiceline DISABLE TRIGGER ALL")
        cur.execute("UPDATE testapp_invoiceline SET amount = 99")
        cur.execute("ALTER TABLE testapp_invoiceline ENABLE TRIGGER ALL")

    refresh_dependent(InvoiceLine.objects.filter(invoice=a))

    a.refresh_from_db()
    b.refresh_from_db()
    assert a.total == 99  # reconciled
    assert b.total == 20  # untouched


# ---------------------------------------------------------------------------
# Multi-hop aggregates — Customer.lifetime_total = Sum("carts__items__amount")
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_multi_hop_customer_total_aggregates_through_carts():
    """Two reverse-FK hops: Customer → carts → items → amount.

    Pins the full contract for multi-hop aggregates: a CartItem write
    propagates up two levels to refresh Customer.lifetime_total.
    """
    customer = Customer.objects.create(name="alice")
    cart = Cart.objects.create(customer=customer)
    CartItem.objects.create(cart=cart, amount=10)
    CartItem.objects.create(cart=cart, amount=15)

    customer.refresh_from_db()
    assert customer.lifetime_total == 25


@pytest.mark.django_db
def test_multi_hop_forward_trigger_recomputes_on_parent_update():
    """The parent's BEFORE UPDATE trigger must compile and execute the
    nested SELECT correctly even before the leaf-table reverse triggers
    are wired up."""
    customer = Customer.objects.create(name="bob")
    cart = Cart.objects.create(customer=customer)
    CartItem.objects.create(cart=cart, amount=10)
    CartItem.objects.create(cart=cart, amount=15)

    customer.save()  # BEFORE UPDATE evaluates the multi-hop aggregate
    customer.refresh_from_db()
    assert customer.lifetime_total == 25


@pytest.mark.django_db
def test_multi_hop_leaf_amount_update_propagates():
    customer = Customer.objects.create(name="alice")
    cart = Cart.objects.create(customer=customer)
    item = CartItem.objects.create(cart=cart, amount=10)
    customer.refresh_from_db()
    assert customer.lifetime_total == 10

    item.amount = 30
    item.save()

    customer.refresh_from_db()
    assert customer.lifetime_total == 30


@pytest.mark.django_db
def test_multi_hop_leaf_delete_propagates():
    customer = Customer.objects.create(name="alice")
    cart = Cart.objects.create(customer=customer)
    a = CartItem.objects.create(cart=cart, amount=10)
    CartItem.objects.create(cart=cart, amount=15)
    customer.refresh_from_db()
    assert customer.lifetime_total == 25

    a.delete()

    customer.refresh_from_db()
    assert customer.lifetime_total == 15


@pytest.mark.django_db
def test_multi_hop_leaf_pivot_across_customers():
    """A CartItem moving from a cart of customer A to a cart of customer B:
    both customers' totals must update through the chain."""
    a = Customer.objects.create(name="alice")
    b = Customer.objects.create(name="bob")
    cart_a = Cart.objects.create(customer=a)
    cart_b = Cart.objects.create(customer=b)
    item = CartItem.objects.create(cart=cart_a, amount=42)
    CartItem.objects.create(cart=cart_b, amount=8)

    a.refresh_from_db()
    b.refresh_from_db()
    assert a.lifetime_total == 42
    assert b.lifetime_total == 8

    item.cart = cart_b
    item.save()

    a.refresh_from_db()
    b.refresh_from_db()
    assert a.lifetime_total == 0
    assert b.lifetime_total == 50


@pytest.mark.django_db
def test_multi_hop_isolated_customers():
    a = Customer.objects.create(name="alice")
    b = Customer.objects.create(name="bob")
    cart_a = Cart.objects.create(customer=a)
    cart_b = Cart.objects.create(customer=b)
    CartItem.objects.create(cart=cart_a, amount=3)
    CartItem.objects.create(cart=cart_b, amount=100)
    CartItem.objects.create(cart=cart_a, amount=4)

    a.refresh_from_db()
    b.refresh_from_db()
    assert a.lifetime_total == 7
    assert b.lifetime_total == 100


@pytest.mark.django_db
def test_multi_hop_intermediate_pivot_between_customers():
    """Cart pivots between customers (Cart.customer_id changes).

    The leaf-table trigger never fires (no CartItem write happens), but
    the intermediate-table UPDATE trigger on Cart fires and recomputes
    both old and new customer totals.
    """
    a = Customer.objects.create(name="alice")
    b = Customer.objects.create(name="bob")
    cart = Cart.objects.create(customer=a)
    CartItem.objects.create(cart=cart, amount=42)
    CartItem.objects.create(cart=cart, amount=8)

    a.refresh_from_db()
    b.refresh_from_db()
    assert a.lifetime_total == 50
    assert b.lifetime_total == 0

    cart.customer = b
    cart.save()

    a.refresh_from_db()
    b.refresh_from_db()
    assert a.lifetime_total == 0
    assert b.lifetime_total == 50


@pytest.mark.django_db
def test_multi_hop_intermediate_delete_propagates():
    """Deleting a Cart cascades to its CartItems. The intermediate-level
    DELETE trigger on Cart recomputes the customer total — the leaf
    DELETE trigger alone can't, because by the time it fires, the Cart
    row it would walk back through is already gone.
    """
    customer = Customer.objects.create(name="alice")
    cart = Cart.objects.create(customer=customer)
    CartItem.objects.create(cart=cart, amount=10)
    CartItem.objects.create(cart=cart, amount=15)
    customer.refresh_from_db()
    assert customer.lifetime_total == 25

    cart.delete()

    customer.refresh_from_db()
    assert customer.lifetime_total == 0


@pytest.mark.django_db
def test_multi_hop_cascade_delete_of_root_does_not_error():
    """Deleting the root cascades through both intermediate and leaf.
    Triggers fire harmlessly: their UPDATE on a deleted root is a no-op."""
    customer = Customer.objects.create(name="alice")
    cart = Cart.objects.create(customer=customer)
    CartItem.objects.create(cart=cart, amount=10)

    customer.delete()

    assert not Customer.objects.exists()
    assert not Cart.objects.exists()
    assert not CartItem.objects.exists()


# ---------------------------------------------------------------------------
# 3-hop sanity (Tenant → Account → Subscription → Charge)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_three_hop_aggregate_compiles_to_triple_nested_select():
    sql = _resolve_reverse_aggregate(
        Sum("accounts__subscriptions__charges__amount"),
        Tenant,
        _q,
        row_ref="NEW",
    )
    assert sql == (
        'COALESCE((SELECT SUM("amount") FROM "testapp_charge" '
        'WHERE "subscription_id" IN ('
        'SELECT "id" FROM "testapp_subscription" '
        'WHERE "account_id" IN ('
        'SELECT "id" FROM "testapp_account" WHERE "tenant_id" = NEW."id"'
        ")"
        ")), 0)"
    )


@pytest.mark.django_db
def test_three_hop_leaf_insert_propagates():
    tenant = Tenant.objects.create(name="acme")
    account = Account.objects.create(tenant=tenant)
    sub = Subscription.objects.create(account=account)
    Charge.objects.create(subscription=sub, amount=10)
    Charge.objects.create(subscription=sub, amount=15)

    tenant.refresh_from_db()
    assert tenant.lifetime_revenue == 25


@pytest.mark.django_db
def test_three_hop_leaf_amount_update_propagates():
    tenant = Tenant.objects.create(name="acme")
    account = Account.objects.create(tenant=tenant)
    sub = Subscription.objects.create(account=account)
    charge = Charge.objects.create(subscription=sub, amount=10)
    tenant.refresh_from_db()
    assert tenant.lifetime_revenue == 10

    charge.amount = 99
    charge.save()

    tenant.refresh_from_db()
    assert tenant.lifetime_revenue == 99


@pytest.mark.django_db
def test_three_hop_intermediate_pivot_at_top_level():
    """Account pivots between Tenants. The Account UPDATE trigger walks
    through one hop (Account.tenant_id) to refresh both old and new
    tenant aggregates."""
    a = Tenant.objects.create(name="acme")
    b = Tenant.objects.create(name="beta")
    account = Account.objects.create(tenant=a)
    sub = Subscription.objects.create(account=account)
    Charge.objects.create(subscription=sub, amount=42)

    a.refresh_from_db()
    b.refresh_from_db()
    assert a.lifetime_revenue == 42
    assert b.lifetime_revenue == 0

    account.tenant = b
    account.save()

    a.refresh_from_db()
    b.refresh_from_db()
    assert a.lifetime_revenue == 0
    assert b.lifetime_revenue == 42


@pytest.mark.django_db
def test_three_hop_intermediate_pivot_at_middle_level():
    """Subscription pivots between Accounts (which belong to different
    Tenants). The Subscription UPDATE trigger walks through two hops
    (Subscription.account_id → Account.tenant_id) to refresh both
    tenants."""
    a = Tenant.objects.create(name="acme")
    b = Tenant.objects.create(name="beta")
    account_a = Account.objects.create(tenant=a)
    account_b = Account.objects.create(tenant=b)
    sub = Subscription.objects.create(account=account_a)
    Charge.objects.create(subscription=sub, amount=100)

    a.refresh_from_db()
    b.refresh_from_db()
    assert a.lifetime_revenue == 100
    assert b.lifetime_revenue == 0

    sub.account = account_b
    sub.save()

    a.refresh_from_db()
    b.refresh_from_db()
    assert a.lifetime_revenue == 0
    assert b.lifetime_revenue == 100


@pytest.mark.django_db
def test_three_hop_intermediate_delete_propagates():
    """Deleting a middle Subscription cascades to its Charges; the
    Subscription DELETE trigger walks two hops up to recompute the
    tenant's revenue (the leaf trigger can't, because the Subscription
    row it would walk through is being deleted in the same statement)."""
    tenant = Tenant.objects.create(name="acme")
    account = Account.objects.create(tenant=tenant)
    sub_keep = Subscription.objects.create(account=account)
    sub_drop = Subscription.objects.create(account=account)
    Charge.objects.create(subscription=sub_keep, amount=10)
    Charge.objects.create(subscription=sub_drop, amount=99)
    tenant.refresh_from_db()
    assert tenant.lifetime_revenue == 109

    sub_drop.delete()

    tenant.refresh_from_db()
    assert tenant.lifetime_revenue == 10


@pytest.mark.django_db
def test_aggregate_cycle_detected():
    """Invoice.total = Sum(lines.amount) and InvoiceLine.amount = F(invoice.total)
    would loop forever; the cycle detector must reject it."""
    invoice_trigger = (
        Invoice,
        GeneratedFieldTrigger(
            field="total",
            expression=Sum("lines__amount"),
            name="fake_invoice_total",
        ),
    )

    from django.db.models import F  # noqa: PLC0415

    line_trigger = (
        InvoiceLine,
        GeneratedFieldTrigger(
            field="amount",
            expression=F("invoice__total"),
            name="fake_line_amount",
        ),
    )

    with pytest.raises(CycleError) as exc_info:
        check_for_cycles([invoice_trigger, line_trigger])

    msg = str(exc_info.value)
    assert "testapp.Invoice.total" in msg
    assert "testapp.InvoiceLine.amount" in msg
