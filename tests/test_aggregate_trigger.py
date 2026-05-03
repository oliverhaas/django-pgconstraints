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
