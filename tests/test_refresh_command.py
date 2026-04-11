"""Tests for the refresh_computed_field management command."""

from decimal import Decimal
from io import StringIO

import pytest
from django.core.management import CommandError, call_command
from django.db import connection
from factories import PartFactory, SupplierFactory

D = Decimal


# ---------------------------------------------------------------------------
# Single-target refresh
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_refresh_single_model_recomputes_corrupted_rows():
    """Raw-SQL-corrupt Part.markup_amount (under disabled triggers), then run the command."""
    supplier = SupplierFactory.create(markup_pct=20)
    part = PartFactory.create(supplier=supplier, base_price=D("100.00"))
    part.refresh_from_db()
    assert part.markup_amount == D("20.00")

    # Corrupt the computed column under disabled triggers so the forward trigger
    # does not recompute the value.
    with connection.cursor() as cur:
        cur.execute("ALTER TABLE testapp_part DISABLE TRIGGER ALL;")
        cur.execute(
            "UPDATE testapp_part SET markup_amount = 999 WHERE id = %s",
            [part.pk],
        )
        cur.execute("ALTER TABLE testapp_part ENABLE TRIGGER ALL;")

    part.refresh_from_db()
    assert part.markup_amount == D("999.00")

    out = StringIO()
    call_command("refresh_computed_field", "testapp.Part", stdout=out)

    part.refresh_from_db()
    assert part.markup_amount == D("20.00")
    assert "testapp.Part" in out.getvalue()


@pytest.mark.django_db(transaction=True)
def test_refresh_all_flag():
    supplier = SupplierFactory.create(markup_pct=20)
    part = PartFactory.create(supplier=supplier, base_price=D("100.00"))

    with connection.cursor() as cur:
        cur.execute("ALTER TABLE testapp_part DISABLE TRIGGER ALL;")
        cur.execute("UPDATE testapp_part SET markup_amount = 0 WHERE id = %s", [part.pk])
        cur.execute("ALTER TABLE testapp_part ENABLE TRIGGER ALL;")

    call_command("refresh_computed_field", "--all", stdout=StringIO())

    part.refresh_from_db()
    assert part.markup_amount == D("20.00")


@pytest.mark.django_db
def test_refresh_unknown_model_raises():
    with pytest.raises(CommandError, match="Model not found"):
        call_command("refresh_computed_field", "testapp.DoesNotExist", stdout=StringIO())


@pytest.mark.django_db
def test_refresh_unknown_field_raises():
    with pytest.raises(CommandError, match="has no GeneratedFieldTrigger"):
        call_command(
            "refresh_computed_field",
            "testapp.Part.not_a_field",
            stdout=StringIO(),
        )


@pytest.mark.django_db
def test_refresh_requires_target_or_all_flag():
    with pytest.raises(CommandError, match="target"):
        call_command("refresh_computed_field", stdout=StringIO())
