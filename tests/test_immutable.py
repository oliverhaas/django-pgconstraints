"""Tests for Immutable constraint."""

from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection
from django.db.models import Q
from testapp.models import Invoice

from django_pgconstraints import Immutable

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
        constraint = Invoice._meta.constraints[0]
        with pytest.raises(ValidationError) as exc_info:
            constraint.validate(Invoice, inv)
        assert exc_info.value.code == "immutable_field"

    def test_validate_allowed_condition_not_met(self):
        inv = Invoice.objects.create(amount=Decimal("100.00"), status="draft")
        inv.amount = Decimal("200.00")
        constraint = Invoice._meta.constraints[0]
        constraint.validate(Invoice, inv)  # should not raise

    def test_validate_new_instance(self):
        inv = Invoice(amount=Decimal("100.00"), status="paid")
        constraint = Invoice._meta.constraints[0]
        constraint.validate(Invoice, inv)  # new instance — always pass

    def test_validate_skips_excluded(self):
        inv = Invoice.objects.create(amount=Decimal("100.00"), status="paid")
        inv.amount = Decimal("200.00")
        constraint = Invoice._meta.constraints[0]
        constraint.validate(Invoice, inv, exclude={"amount"})  # should not raise


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------


class TestImmutableDeconstruct:
    def test_deconstruct_with_when(self):
        when = Q(status="paid")
        constraint = Immutable(fields=["amount"], when=when, name="c")
        path, args, kwargs = constraint.deconstruct()
        assert path == "django_pgconstraints.Immutable"
        assert args == ()
        assert kwargs["fields"] == ["amount"]
        assert kwargs["when"] == when
        assert kwargs["name"] == "c"

    def test_deconstruct_without_when(self):
        constraint = Immutable(fields=["amount"], name="c")
        _, _, kwargs = constraint.deconstruct()
        assert "when" not in kwargs

    def test_roundtrip(self):
        original = Immutable(fields=["amount", "status"], when=Q(locked=True), name="c")
        _, args, kwargs = original.deconstruct()
        restored = Immutable(*args, **kwargs)
        assert original == restored


# ---------------------------------------------------------------------------
# Trigger lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
class TestImmutableLifecycle:
    def _trigger_exists(self, name, table):
        with connection.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM information_schema.triggers WHERE trigger_name = %s AND event_object_table = %s",
                [name, table],
            )
            return cur.fetchone() is not None

    def test_trigger_created(self):
        assert self._trigger_exists("testapp_invoice_immutable_amount_when_paid", "testapp_invoice")

    def test_remove_and_recreate(self):
        constraint = Invoice._meta.constraints[0]
        with connection.schema_editor() as editor:
            editor.remove_constraint(Invoice, constraint)
        assert not self._trigger_exists("testapp_invoice_immutable_amount_when_paid", "testapp_invoice")

        with connection.schema_editor() as editor:
            editor.add_constraint(Invoice, constraint)
        assert self._trigger_exists("testapp_invoice_immutable_amount_when_paid", "testapp_invoice")
