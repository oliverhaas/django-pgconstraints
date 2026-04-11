"""Comprehensive CheckConstraintTrigger tests.

Test matrix:
- Local-only condition (same as Django's CheckConstraint)
- FK-traversal condition (our addition)
- validate() for local conditions
- validate() skips FK-traversal conditions
- Construction validation
- Condition with F() same-table comparisons
- Combined Q objects (AND, OR, NOT)
"""

from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection
from django.db.models import F, Q
from testapp.models import OrderLine, Product

from django_pgconstraints import CheckConstraintTrigger

D = Decimal


# ---------------------------------------------------------------------------
# Local-only condition (equivalent to Django's CheckConstraint)
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
class TestLocalCondition:
    """Simple same-table conditions — behaves like CheckConstraint."""

    def test_positive_quantity_passes(self):
        product = Product.objects.create(name="Widget", stock=10)
        OrderLine.objects.create(product=product, quantity=1)

    def test_zero_quantity_blocked(self):
        product = Product.objects.create(name="Widget", stock=10)
        with pytest.raises(IntegrityError):
            OrderLine.objects.create(product=product, quantity=0)

    def test_negative_quantity_blocked(self):
        product = Product.objects.create(name="Widget", stock=10)
        with pytest.raises(IntegrityError):
            OrderLine.objects.create(product=product, quantity=-5)

    def test_update_to_invalid_blocked(self):
        product = Product.objects.create(name="Widget", stock=10)
        line = OrderLine.objects.create(product=product, quantity=5)
        line.quantity = 0
        with pytest.raises(IntegrityError):
            line.save()


# ---------------------------------------------------------------------------
# FK-traversal condition (our addition)
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
class TestFKTraversalCondition:
    """Conditions referencing related models via F("product__stock")."""

    def test_within_stock_passes(self):
        product = Product.objects.create(name="Widget", stock=10)
        OrderLine.objects.create(product=product, quantity=5)

    def test_at_stock_limit_passes(self):
        product = Product.objects.create(name="Widget", stock=10)
        OrderLine.objects.create(product=product, quantity=10)

    def test_exceeding_stock_blocked(self):
        product = Product.objects.create(name="Widget", stock=5)
        with pytest.raises(IntegrityError):
            OrderLine.objects.create(product=product, quantity=6)

    def test_update_to_exceed_blocked(self):
        product = Product.objects.create(name="Widget", stock=10)
        line = OrderLine.objects.create(product=product, quantity=5)
        line.quantity = 11
        with pytest.raises(IntegrityError):
            line.save()

    def test_reassign_product_rechecks(self):
        """Changing the FK re-checks against the new product's stock."""
        big = Product.objects.create(name="Big", stock=100)
        small = Product.objects.create(name="Small", stock=2)
        line = OrderLine.objects.create(product=big, quantity=50)
        line.product = small
        with pytest.raises(IntegrityError):
            line.save()

    def test_multiple_products_independent(self):
        """Each line is checked against its own product's stock."""
        big = Product.objects.create(name="Big", stock=100)
        small = Product.objects.create(name="Small", stock=5)
        OrderLine.objects.create(product=big, quantity=50)
        OrderLine.objects.create(product=small, quantity=5)
        with pytest.raises(IntegrityError):
            OrderLine.objects.create(product=small, quantity=6)


# ---------------------------------------------------------------------------
# Python-level validation
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
class TestValidation:
    """validate() works for local conditions, skips FK-traversal."""

    def test_local_condition_validates(self):
        """Local-only condition (quantity > 0) is checked in Python."""
        product = Product.objects.create(name="Widget", stock=10)
        line = OrderLine(product=product, quantity=0)
        trigger = OrderLine._meta.triggers[1]  # qty_positive
        with pytest.raises(ValidationError):
            trigger.validate(OrderLine, line)

    def test_local_condition_passes(self):
        product = Product.objects.create(name="Widget", stock=10)
        line = OrderLine(product=product, quantity=5)
        trigger = OrderLine._meta.triggers[1]  # qty_positive
        trigger.validate(OrderLine, line)  # should not raise

    def test_fk_condition_skipped(self):
        """FK-traversal condition is not checked in Python — trigger handles it."""
        product = Product.objects.create(name="Widget", stock=2)
        line = OrderLine(product=product, quantity=100)
        trigger = OrderLine._meta.triggers[0]  # qty_lte_stock
        # This should NOT raise even though quantity > stock,
        # because FK-traversal conditions skip Python validation.
        trigger.validate(OrderLine, line)


# ---------------------------------------------------------------------------
# Dynamic trigger install (combined Q, F-to-F comparison)
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
class TestDynamicConditions:
    """Conditions installed dynamically to test various Q patterns."""

    def test_f_to_f_same_table(self):
        """F-to-F comparison: quantity <= max_order_quantity (both local)."""
        trigger = CheckConstraintTrigger(
            condition=Q(quantity__lte=F("product__max_order_quantity")),
            name="orderline_qty_lte_max",
        )
        trigger.install(OrderLine)
        try:
            product = Product.objects.create(name="W", stock=1000, max_order_quantity=10)
            OrderLine.objects.create(product=product, quantity=10)
            with pytest.raises(IntegrityError):
                OrderLine.objects.create(product=product, quantity=11)
        finally:
            trigger.uninstall(OrderLine)

    def test_negated_condition(self):
        """~Q(quantity=0) — quantity must not be zero."""
        trigger = CheckConstraintTrigger(
            condition=~Q(quantity=0),
            name="orderline_qty_not_zero",
        )
        trigger.install(OrderLine)
        try:
            product = Product.objects.create(name="W", stock=10)
            OrderLine.objects.create(product=product, quantity=1)
            with pytest.raises(IntegrityError):
                OrderLine.objects.create(product=product, quantity=0)
        finally:
            trigger.uninstall(OrderLine)

    def test_or_condition(self):
        """Q(quantity=1) | Q(quantity=5) — only 1 or 5 allowed."""
        trigger = CheckConstraintTrigger(
            condition=Q(quantity=1) | Q(quantity=5),
            name="orderline_qty_1_or_5",
        )
        trigger.install(OrderLine)
        try:
            product = Product.objects.create(name="W", stock=10)
            OrderLine.objects.create(product=product, quantity=1)
            OrderLine.objects.create(product=product, quantity=5)
            with pytest.raises(IntegrityError):
                OrderLine.objects.create(product=product, quantity=3)
        finally:
            trigger.uninstall(OrderLine)

    def test_and_condition(self):
        """Q(quantity__gt=0) & Q(quantity__lte=100) — range check."""
        trigger = CheckConstraintTrigger(
            condition=Q(quantity__gt=0) & Q(quantity__lte=100),
            name="orderline_qty_range",
        )
        trigger.install(OrderLine)
        try:
            product = Product.objects.create(name="W", stock=1000)
            OrderLine.objects.create(product=product, quantity=1)
            OrderLine.objects.create(product=product, quantity=100)
            with pytest.raises(IntegrityError):
                OrderLine.objects.create(product=product, quantity=0)
            with pytest.raises(IntegrityError):
                OrderLine.objects.create(product=product, quantity=101)
        finally:
            trigger.uninstall(OrderLine)


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_condition_stored(self):
        q = Q(quantity__gt=0)
        t = CheckConstraintTrigger(condition=q, name="c")
        assert t.check_condition is q

    def test_non_q_raises(self):
        with pytest.raises(TypeError, match="must be a Q instance"):
            CheckConstraintTrigger(condition="not a Q", name="c")  # type: ignore[arg-type]

    def test_custom_error_code(self):
        t = CheckConstraintTrigger(
            condition=Q(quantity__gt=0),
            violation_error_code="custom",
            name="c",
        )
        assert t.violation_error_code == "custom"

    def test_custom_error_message(self):
        t = CheckConstraintTrigger(
            condition=Q(quantity__gt=0),
            violation_error_message="Bad value",
            name="c",
        )
        assert t.violation_error_message == "Bad value"

    def test_default_error_message_includes_name(self):
        t = CheckConstraintTrigger(condition=Q(x__gt=0), name="my_check")
        assert "my_check" in t.get_violation_error_message()


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
class TestLifecycle:
    def _trigger_exists(self, name_fragment, table):
        with connection.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM pg_trigger t "
                "JOIN pg_class c ON t.tgrelid = c.oid "
                "WHERE t.tgname LIKE %s AND c.relname = %s",
                [f"%{name_fragment}%", table],
            )
            return cur.fetchone() is not None

    def test_triggers_created(self):
        assert self._trigger_exists("orderline_qty_lte_stock", "testapp_orderline")
        assert self._trigger_exists("orderline_qty_positive", "testapp_orderline")

    def test_remove_and_recreate(self):
        trigger = OrderLine._meta.triggers[0]
        trigger.uninstall(OrderLine)
        assert not self._trigger_exists("orderline_qty_lte_stock", "testapp_orderline")

        trigger.install(OrderLine)
        assert self._trigger_exists("orderline_qty_lte_stock", "testapp_orderline")

    def test_install_is_idempotent(self):
        trigger = OrderLine._meta.triggers[0]
        trigger.uninstall(OrderLine)
        trigger.install(OrderLine)
        trigger.install(OrderLine)
        assert self._trigger_exists("orderline_qty_lte_stock", "testapp_orderline")
