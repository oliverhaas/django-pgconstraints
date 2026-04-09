"""Tests for AllowedTransitions constraint."""

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection
from testapp.models import Order

from django_pgconstraints import AllowedTransitions

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
        constraint = Order._meta.constraints[0]
        with pytest.raises(ValidationError) as exc_info:
            constraint.validate(Order, order)
        assert exc_info.value.code == "invalid_transition"

    def test_validate_allowed(self):
        order = Order.objects.create(status="draft")
        order.status = "pending"
        constraint = Order._meta.constraints[0]
        constraint.validate(Order, order)  # should not raise

    def test_validate_new_instance(self):
        order = Order(status="shipped")
        constraint = Order._meta.constraints[0]
        constraint.validate(Order, order)  # new instances always pass

    def test_validate_same_value(self):
        order = Order.objects.create(status="draft")
        order.status = "draft"
        constraint = Order._meta.constraints[0]
        constraint.validate(Order, order)  # no change — always pass

    def test_validate_skips_excluded(self):
        order = Order.objects.create(status="draft")
        order.status = "shipped"
        constraint = Order._meta.constraints[0]
        constraint.validate(Order, order, exclude={"status"})  # should not raise


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------


class TestAllowedTransitionsDeconstruct:
    def test_deconstruct(self):
        transitions = {"draft": ["pending"], "pending": ["shipped"]}
        constraint = AllowedTransitions(field="status", transitions=transitions, name="c")
        path, args, kwargs = constraint.deconstruct()
        assert path == "django_pgconstraints.AllowedTransitions"
        assert args == ()
        assert kwargs["field"] == "status"
        assert kwargs["transitions"] == transitions
        assert kwargs["name"] == "c"

    def test_roundtrip(self):
        transitions = {"draft": ["pending"], "pending": ["shipped"]}
        original = AllowedTransitions(field="status", transitions=transitions, name="c")
        _, args, kwargs = original.deconstruct()
        restored = AllowedTransitions(*args, **kwargs)
        assert original == restored


# ---------------------------------------------------------------------------
# Trigger lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
class TestAllowedTransitionsLifecycle:
    def _trigger_exists(self, name, table):
        with connection.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM information_schema.triggers WHERE trigger_name = %s AND event_object_table = %s",
                [name, table],
            )
            return cur.fetchone() is not None

    def test_trigger_created(self):
        assert self._trigger_exists("testapp_order_status_transitions", "testapp_order")

    def test_remove_and_recreate(self):
        constraint = Order._meta.constraints[0]
        with connection.schema_editor() as editor:
            editor.remove_constraint(Order, constraint)
        assert not self._trigger_exists("testapp_order_status_transitions", "testapp_order")

        with connection.schema_editor() as editor:
            editor.add_constraint(Order, constraint)
        assert self._trigger_exists("testapp_order_status_transitions", "testapp_order")
