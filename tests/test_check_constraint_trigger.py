"""Tests for CheckConstraintTrigger."""

import pytest
from django.db import IntegrityError, connection
from testapp.models import OrderLine, Product

# ---------------------------------------------------------------------------
# DB-level enforcement
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
class TestCheckConstraintTriggerEnforcement:
    def test_insert_within_stock(self):
        product = Product.objects.create(name="Widget", stock=10)
        OrderLine.objects.create(product=product, quantity=5)

    def test_insert_at_stock_limit(self):
        product = Product.objects.create(name="Widget", stock=10)
        OrderLine.objects.create(product=product, quantity=10)

    def test_insert_exceeding_stock(self):
        product = Product.objects.create(name="Widget", stock=5)
        with pytest.raises(IntegrityError):
            OrderLine.objects.create(product=product, quantity=6)

    def test_update_to_exceed_stock(self):
        product = Product.objects.create(name="Widget", stock=10)
        line = OrderLine.objects.create(product=product, quantity=5)
        line.quantity = 11
        with pytest.raises(IntegrityError):
            line.save()

    def test_update_within_stock(self):
        product = Product.objects.create(name="Widget", stock=10)
        line = OrderLine.objects.create(product=product, quantity=5)
        line.quantity = 8
        line.save()

    def test_local_only_check(self):
        """The quantity > 0 constraint is a simple local check."""
        product = Product.objects.create(name="Widget", stock=10)
        with pytest.raises(IntegrityError):
            OrderLine.objects.create(product=product, quantity=0)

    def test_local_check_passes(self):
        product = Product.objects.create(name="Widget", stock=10)
        OrderLine.objects.create(product=product, quantity=1)

    def test_reassign_product(self):
        """Changing the FK should re-check against the new product's stock."""
        big = Product.objects.create(name="Big", stock=100)
        small = Product.objects.create(name="Small", stock=2)
        line = OrderLine.objects.create(product=big, quantity=50)
        line.product = small
        with pytest.raises(IntegrityError):
            line.save()


# ---------------------------------------------------------------------------
# Trigger lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
class TestCheckConstraintTriggerLifecycle:
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

    def test_remove_and_recreate(self):
        trigger = OrderLine._meta.triggers[0]
        trigger.uninstall(OrderLine)
        assert not self._trigger_exists("orderline_qty_lte_stock", "testapp_orderline")

        trigger.install(OrderLine)
        assert self._trigger_exists("orderline_qty_lte_stock", "testapp_orderline")
