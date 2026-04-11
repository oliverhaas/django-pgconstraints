"""Tests for AllowedTransitions constraint."""

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection
from testapp.models import Order

from django_pgconstraints import validate_allowed_transition

TRANSITIONS = {
    "draft": ["pending"],
    "pending": ["shipped"],
    "shipped": ["delivered"],
}

# ---------------------------------------------------------------------------
# DB-level enforcement
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
class TestAllowedTransitionsEnforcement:
    def test_allowed_transition(self):
        order = Order.objects.create(status="draft")
        order.status = "pending"
        order.save()

    def test_multi_step_transitions(self):
        order = Order.objects.create(status="draft")
        order.status = "pending"
        order.save()
        order.status = "shipped"
        order.save()
        order.status = "delivered"
        order.save()

    def test_disallowed_transition(self):
        order = Order.objects.create(status="draft")
        order.status = "shipped"
        with pytest.raises(IntegrityError):
            order.save()

    def test_no_transition_from_terminal_state(self):
        """A state not in the transitions dict allows no outgoing transitions."""
        order = Order.objects.create(status="delivered")
        order.status = "draft"
        with pytest.raises(IntegrityError):
            order.save()

    def test_same_value_no_op(self):
        """Setting the same value is not a transition — always allowed."""
        order = Order.objects.create(status="pending")
        order.status = "pending"
        order.save()  # should not raise

    def test_insert_any_state(self):
        """Inserts are unconstrained — the trigger only fires on UPDATE."""
        Order.objects.create(status="delivered")
        Order.objects.create(status="shipped")
        Order.objects.create(status="draft")


# ---------------------------------------------------------------------------
# Python-level validation
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
class TestAllowedTransitionsValidation:
    def test_validate_disallowed(self):
        order = Order.objects.create(status="draft")
        order.status = "shipped"
        with pytest.raises(ValidationError) as exc_info:
            validate_allowed_transition(instance=order, field="status", transitions=TRANSITIONS)
        assert exc_info.value.code == "invalid_transition"

    def test_validate_allowed(self):
        order = Order.objects.create(status="draft")
        order.status = "pending"
        validate_allowed_transition(instance=order, field="status", transitions=TRANSITIONS)  # should not raise

    def test_validate_new_instance(self):
        order = Order(status="shipped")
        validate_allowed_transition(
            instance=order,
            field="status",
            transitions=TRANSITIONS,
        )  # new instances always pass

    def test_validate_same_value(self):
        order = Order.objects.create(status="draft")
        order.status = "draft"
        validate_allowed_transition(instance=order, field="status", transitions=TRANSITIONS)  # no change — always pass


# ---------------------------------------------------------------------------
# Trigger lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
class TestAllowedTransitionsLifecycle:
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
        assert self._trigger_exists("order_status_transitions", "testapp_order")

    def test_remove_and_recreate(self):
        trigger = Order._meta.triggers[0]
        trigger.uninstall(Order)
        assert not self._trigger_exists("order_status_transitions", "testapp_order")

        trigger.install(Order)
        assert self._trigger_exists("order_status_transitions", "testapp_order")
