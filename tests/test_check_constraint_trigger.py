"""Tests for CheckConstraintTrigger.

Covers: local-only conditions, FK-traversal conditions, validate(),
dynamic Q patterns (AND/OR/NOT/F-to-F), construction, and lifecycle.
"""

from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.db.models import F, Q
from factories import OrderLineFactory, ProductFactory
from helpers import swap_trigger, trigger_exists
from testapp.models import OrderLine

from django_pgconstraints import CheckConstraintTrigger

D = Decimal


# ---------------------------------------------------------------------------
# Local-only condition (same semantics as Django's CheckConstraint)
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_local_positive_quantity_passes():
    product = ProductFactory.create(stock=10)
    OrderLineFactory.create(product=product, quantity=1)


@pytest.mark.django_db(transaction=True)
def test_local_zero_quantity_blocked():
    product = ProductFactory.create(stock=10)
    with pytest.raises(IntegrityError):
        OrderLineFactory.create(product=product, quantity=0)


@pytest.mark.django_db(transaction=True)
def test_local_negative_quantity_blocked():
    product = ProductFactory.create(stock=10)
    with pytest.raises(IntegrityError):
        OrderLineFactory.create(product=product, quantity=-5)


@pytest.mark.django_db(transaction=True)
def test_local_update_to_invalid_blocked():
    product = ProductFactory.create(stock=10)
    line = OrderLineFactory.create(product=product, quantity=5)
    line.quantity = 0
    with pytest.raises(IntegrityError):
        line.save()


# ---------------------------------------------------------------------------
# FK-traversal condition (quantity <= product__stock)
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_fk_within_stock_passes():
    product = ProductFactory.create(stock=10)
    OrderLineFactory.create(product=product, quantity=5)


@pytest.mark.django_db(transaction=True)
def test_fk_at_stock_limit_passes():
    product = ProductFactory.create(stock=10)
    OrderLineFactory.create(product=product, quantity=10)


@pytest.mark.django_db(transaction=True)
def test_fk_exceeding_stock_blocked():
    product = ProductFactory.create(stock=5)
    with pytest.raises(IntegrityError):
        OrderLineFactory.create(product=product, quantity=6)


@pytest.mark.django_db(transaction=True)
def test_fk_update_to_exceed_stock_blocked():
    product = ProductFactory.create(stock=10)
    line = OrderLineFactory.create(product=product, quantity=5)
    line.quantity = 11
    with pytest.raises(IntegrityError):
        line.save()


@pytest.mark.django_db(transaction=True)
def test_fk_reassign_product_rechecks():
    big = ProductFactory.create(stock=100)
    small = ProductFactory.create(stock=2)
    line = OrderLineFactory.create(product=big, quantity=50)
    line.product = small
    with pytest.raises(IntegrityError):
        line.save()


@pytest.mark.django_db(transaction=True)
def test_fk_multiple_products_independent():
    big = ProductFactory.create(stock=100)
    small = ProductFactory.create(stock=5)
    OrderLineFactory.create(product=big, quantity=50)
    OrderLineFactory.create(product=small, quantity=5)
    with pytest.raises(IntegrityError):
        OrderLineFactory.create(product=small, quantity=6)


# ---------------------------------------------------------------------------
# validate() — Python-level validation
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_validate_local_condition_raises():
    """Local-only condition (quantity > 0) is checked in Python."""
    product = ProductFactory.create(stock=10)
    line = OrderLine(product=product, quantity=0)
    trigger = OrderLine._meta.triggers[1]  # qty_positive
    with pytest.raises(ValidationError):
        trigger.validate(OrderLine, line)


@pytest.mark.django_db(transaction=True)
def test_validate_local_condition_passes():
    product = ProductFactory.create(stock=10)
    line = OrderLine(product=product, quantity=5)
    trigger = OrderLine._meta.triggers[1]
    trigger.validate(OrderLine, line)


@pytest.mark.django_db(transaction=True)
def test_validate_fk_condition_skipped():
    """FK-traversal conditions are skipped in Python — trigger handles them."""
    product = ProductFactory.create(stock=2)
    line = OrderLine(product=product, quantity=100)
    trigger = OrderLine._meta.triggers[0]  # qty_lte_stock
    trigger.validate(OrderLine, line)


# ---------------------------------------------------------------------------
# Dynamic Q patterns (swap a custom trigger in and out)
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_dynamic_f_to_f_same_table():
    """quantity <= product__max_order_quantity — both sides resolve via the same FK."""
    trigger = CheckConstraintTrigger(
        condition=Q(quantity__lte=F("product__max_order_quantity")),
        name="orderline_qty_lte_max",
    )
    # Swap out the stock-bound trigger (index 0) so it doesn't also fire.
    with swap_trigger(OrderLine, trigger, index=0):
        product = ProductFactory.create(stock=1000, max_order_quantity=10)
        OrderLineFactory.create(product=product, quantity=10)
        with pytest.raises(IntegrityError):
            OrderLineFactory.create(product=product, quantity=11)


@pytest.mark.django_db(transaction=True)
def test_dynamic_negated_condition():
    trigger = CheckConstraintTrigger(
        condition=~Q(quantity=0),
        name="orderline_qty_not_zero",
    )
    with swap_trigger(OrderLine, trigger, index=1):  # swap qty_positive
        product = ProductFactory.create(stock=10)
        OrderLineFactory.create(product=product, quantity=1)
        with pytest.raises(IntegrityError):
            OrderLineFactory.create(product=product, quantity=0)


@pytest.mark.django_db(transaction=True)
def test_dynamic_or_condition():
    trigger = CheckConstraintTrigger(
        condition=Q(quantity=1) | Q(quantity=5),
        name="orderline_qty_1_or_5",
    )
    with swap_trigger(OrderLine, trigger, index=1):
        product = ProductFactory.create(stock=10)
        OrderLineFactory.create(product=product, quantity=1)
        OrderLineFactory.create(product=product, quantity=5)
        with pytest.raises(IntegrityError):
            OrderLineFactory.create(product=product, quantity=3)


@pytest.mark.django_db(transaction=True)
def test_dynamic_and_condition():
    trigger = CheckConstraintTrigger(
        condition=Q(quantity__gt=0) & Q(quantity__lte=100),
        name="orderline_qty_range",
    )
    with swap_trigger(OrderLine, trigger, index=1):
        product = ProductFactory.create(stock=1000)
        OrderLineFactory.create(product=product, quantity=1)
        OrderLineFactory.create(product=product, quantity=100)
        with pytest.raises(IntegrityError):
            OrderLineFactory.create(product=product, quantity=0)
        with pytest.raises(IntegrityError):
            OrderLineFactory.create(product=product, quantity=101)


# ---------------------------------------------------------------------------
# Rich lookups (proof that Django's full lookup machinery is wired up)
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_lookup_range():
    trigger = CheckConstraintTrigger(
        condition=Q(quantity__range=(1, 10)),
        name="orderline_qty_range_lookup",
    )
    with swap_trigger(OrderLine, trigger, index=1):
        product = ProductFactory.create(stock=1000)
        OrderLineFactory.create(product=product, quantity=1)
        OrderLineFactory.create(product=product, quantity=10)
        with pytest.raises(IntegrityError):
            OrderLineFactory.create(product=product, quantity=11)


@pytest.mark.django_db(transaction=True)
def test_lookup_in_values():
    trigger = CheckConstraintTrigger(
        condition=Q(quantity__in=[1, 3, 5]),
        name="orderline_qty_in_set",
    )
    with swap_trigger(OrderLine, trigger, index=1):
        product = ProductFactory.create(stock=100)
        OrderLineFactory.create(product=product, quantity=3)
        with pytest.raises(IntegrityError):
            OrderLineFactory.create(product=product, quantity=2)


@pytest.mark.django_db(transaction=True)
def test_lookup_fk_startswith():
    """FK-traversed CharField + string lookup — product__name__startswith."""
    trigger = CheckConstraintTrigger(
        condition=Q(product__name__startswith="Widget"),
        name="orderline_product_name_starts_widget",
    )
    with swap_trigger(OrderLine, trigger, index=1):
        ok = ProductFactory.create(name="Widget Deluxe", stock=100)
        bad = ProductFactory.create(name="Gadget", stock=100)
        OrderLineFactory.create(product=ok, quantity=1)
        with pytest.raises(IntegrityError):
            OrderLineFactory.create(product=bad, quantity=1)


@pytest.mark.django_db(transaction=True)
def test_lookup_fk_iexact():
    trigger = CheckConstraintTrigger(
        condition=Q(product__name__iexact="widget"),
        name="orderline_product_name_iexact",
    )
    with swap_trigger(OrderLine, trigger, index=1):
        ok = ProductFactory.create(name="WIDGET", stock=100)
        bad = ProductFactory.create(name="Gadget", stock=100)
        OrderLineFactory.create(product=ok, quantity=1)
        with pytest.raises(IntegrityError):
            OrderLineFactory.create(product=bad, quantity=1)


@pytest.mark.django_db(transaction=True)
def test_lookup_fk_icontains():
    trigger = CheckConstraintTrigger(
        condition=Q(product__name__icontains="widget"),
        name="orderline_product_name_icontains",
    )
    with swap_trigger(OrderLine, trigger, index=1):
        ok = ProductFactory.create(name="Super-WIDGET-Pro", stock=100)
        bad = ProductFactory.create(name="Gadget", stock=100)
        OrderLineFactory.create(product=ok, quantity=1)
        with pytest.raises(IntegrityError):
            OrderLineFactory.create(product=bad, quantity=1)


@pytest.mark.django_db(transaction=True)
def test_lookup_fk_regex():
    trigger = CheckConstraintTrigger(
        condition=Q(product__name__regex=r"^[A-Z][a-z]+$"),
        name="orderline_product_name_regex",
    )
    with swap_trigger(OrderLine, trigger, index=1):
        ok = ProductFactory.create(name="Widget", stock=100)
        bad = ProductFactory.create(name="WIDGET-pro", stock=100)
        OrderLineFactory.create(product=ok, quantity=1)
        with pytest.raises(IntegrityError):
            OrderLineFactory.create(product=bad, quantity=1)


@pytest.mark.django_db(transaction=True)
def test_lookup_decimal_value_preserves_precision():
    """Decimal comparisons — proves Django's field-aware prep_value is called."""
    trigger = CheckConstraintTrigger(
        condition=Q(product__stock__gte=1),
        name="orderline_product_stock_gte_one",
    )
    with swap_trigger(OrderLine, trigger, index=0):
        big = ProductFactory.create(stock=5)
        zero = ProductFactory.create(stock=0)
        OrderLineFactory.create(product=big, quantity=1)
        with pytest.raises(IntegrityError):
            OrderLineFactory.create(product=zero, quantity=1)


# ---------------------------------------------------------------------------
# Construction (pure Python, no DB)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_construction_condition_stored():
    q = Q(quantity__gt=0)
    t = CheckConstraintTrigger(condition=q, name="c")
    assert t.check_condition is q


@pytest.mark.unit
def test_construction_non_q_raises():
    with pytest.raises(TypeError, match="must be a Q instance"):
        CheckConstraintTrigger(condition="not a Q", name="c")  # type: ignore[arg-type]


@pytest.mark.unit
def test_construction_custom_error_code():
    t = CheckConstraintTrigger(
        condition=Q(quantity__gt=0),
        violation_error_code="custom",
        name="c",
    )
    assert t.violation_error_code == "custom"


@pytest.mark.unit
def test_construction_custom_error_message():
    t = CheckConstraintTrigger(
        condition=Q(quantity__gt=0),
        violation_error_message="Bad value",
        name="c",
    )
    assert t.violation_error_message == "Bad value"


@pytest.mark.unit
def test_construction_default_error_message_includes_name():
    t = CheckConstraintTrigger(condition=Q(x__gt=0), name="my_check")
    assert "my_check" in t.get_violation_error_message()


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_lifecycle_both_triggers_created():
    assert trigger_exists("orderline_qty_lte_stock", "testapp_orderline")
    assert trigger_exists("orderline_qty_positive", "testapp_orderline")


@pytest.mark.django_db(transaction=True)
def test_lifecycle_remove_and_recreate():
    trigger = OrderLine._meta.triggers[0]
    trigger.uninstall(OrderLine)
    assert not trigger_exists("orderline_qty_lte_stock", "testapp_orderline")
    trigger.install(OrderLine)
    assert trigger_exists("orderline_qty_lte_stock", "testapp_orderline")


@pytest.mark.django_db(transaction=True)
def test_lifecycle_install_is_idempotent():
    trigger = OrderLine._meta.triggers[0]
    trigger.uninstall(OrderLine)
    trigger.install(OrderLine)
    trigger.install(OrderLine)
    assert trigger_exists("orderline_qty_lte_stock", "testapp_orderline")
