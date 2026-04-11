"""Tests for Immutable constraint."""

from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection
from django.db.models import Q
from testapp.models import Invoice

from django_pgconstraints import Immutable, validate_immutable

# ---------------------------------------------------------------------------
# DB-level enforcement
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
class TestImmutableEnforcement:
    def test_change_blocked_when_condition_met(self):
        inv = Invoice.objects.create(amount=Decimal("100.00"), status="paid")
        inv.amount = Decimal("200.00")
        with pytest.raises(IntegrityError):
            inv.save()

    def test_change_allowed_when_condition_not_met(self):
        inv = Invoice.objects.create(amount=Decimal("100.00"), status="draft")
        inv.amount = Decimal("200.00")
        inv.save()  # should not raise

    def test_unrelated_field_change_allowed(self):
        """Changing a field NOT listed in 'fields' is always allowed."""
        inv = Invoice.objects.create(amount=Decimal("100.00"), status="paid")
        inv.status = "refunded"
        inv.save()  # should not raise — only amount is immutable

    def test_transition_to_paid_with_amount_change(self):
        """Can change amount while transitioning TO the immutable state."""
        inv = Invoice.objects.create(amount=Decimal("100.00"), status="draft")
        inv.status = "paid"
        inv.amount = Decimal("150.00")
        inv.save()  # OLD.status was 'draft', so amount is still mutable

    def test_no_change_no_violation(self):
        """Setting the same amount on a paid invoice is fine."""
        inv = Invoice.objects.create(amount=Decimal("100.00"), status="paid")
        inv.amount = Decimal("100.00")
        inv.save()  # same value — IS DISTINCT FROM is false


# ---------------------------------------------------------------------------
# Python-level validation
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
class TestImmutableValidation:
    def test_validate_blocked(self):
        inv = Invoice.objects.create(amount=Decimal("100.00"), status="paid")
        inv.amount = Decimal("200.00")
        with pytest.raises(ValidationError) as exc_info:
            validate_immutable(instance=inv, fields=["amount"], when=Q(status="paid"))
        assert exc_info.value.code == "immutable_field"

    def test_validate_allowed_condition_not_met(self):
        inv = Invoice.objects.create(amount=Decimal("100.00"), status="draft")
        inv.amount = Decimal("200.00")
        validate_immutable(instance=inv, fields=["amount"], when=Q(status="paid"))  # should not raise

    def test_validate_new_instance(self):
        inv = Invoice(amount=Decimal("100.00"), status="paid")
        validate_immutable(instance=inv, fields=["amount"], when=Q(status="paid"))  # new instance — always pass


# ---------------------------------------------------------------------------
# Init validation
# ---------------------------------------------------------------------------


class TestImmutableInit:
    def test_empty_fields_raises(self):
        with pytest.raises(ValueError):
            Immutable(fields=[], name="c")


# ---------------------------------------------------------------------------
# Trigger lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
class TestImmutableLifecycle:
    def _trigger_exists(self, name_fragment, table):
        with connection.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM pg_trigger t "
                "JOIN pg_class c ON t.tgrelid = c.oid "
                "WHERE t.tgname LIKE %s AND c.relname = %s",
                [f"%{name_fragment}%", table],
            )
            return cur.fetchone() is not None

    def test_trigger_created(self):
        assert self._trigger_exists("invoice_immutable_amount_when_paid", "testapp_invoice")

    def test_remove_and_recreate(self):
        trigger = Invoice._meta.triggers[0]
        trigger.uninstall(Invoice)
        assert not self._trigger_exists("invoice_immutable_amount_when_paid", "testapp_invoice")

        trigger.install(Invoice)
        assert self._trigger_exists("invoice_immutable_amount_when_paid", "testapp_invoice")
