"""GeneratedFieldTrigger over a reverse relation aggregate (Sum, Count, ...).

The goal: ``GeneratedFieldTrigger(expression=Sum("lines__amount"))`` keeps
``Invoice.total`` in sync as ``InvoiceLine`` rows are inserted, deleted,
updated, or moved between invoices.

Each test in this module pins down one slice of that contract. They land
red and turn green as the implementation grows.
"""

import pytest

from testapp.models import Invoice, InvoiceLine


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
    InvoiceLine.objects.bulk_create([
        InvoiceLine(invoice=invoice, amount=5),
        InvoiceLine(invoice=invoice, amount=7),
        InvoiceLine(invoice=invoice, amount=11),
    ])

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
