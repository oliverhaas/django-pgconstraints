"""Tests for UniqueConstraintTrigger."""

import threading

import psycopg
import pytest
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection
from django.db.models import Deferrable
from django.db.models.functions import Left, Lower
from testapp.models import Page

from django_pgconstraints import UniqueConstraintTrigger

# ---------------------------------------------------------------------------
# DB-level enforcement
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
class TestUniqueConstraintTriggerEnforcement:
    def test_insert_duplicate_blocked(self):
        Page.objects.create(slug="hello")
        with pytest.raises(IntegrityError):
            Page.objects.create(slug="hello")

    def test_different_values_allowed(self):
        Page.objects.create(slug="alpha")
        Page.objects.create(slug="beta")

    def test_update_to_duplicate_blocked(self):
        Page.objects.create(slug="taken")
        page = Page.objects.create(slug="free")
        page.slug = "taken"
        with pytest.raises(IntegrityError):
            page.save()

    def test_update_same_value_allowed(self):
        page = Page.objects.create(slug="mine")
        page.slug = "mine"
        page.save()  # no-op update, should not raise

    def test_null_values_allowed(self):
        """NULLs are distinct by default — multiple NULLs don't conflict."""
        Page.objects.create(slug=None)
        Page.objects.create(slug=None)


# ---------------------------------------------------------------------------
# Python-level validation
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
class TestUniqueConstraintTriggerValidation:
    def test_validate_raises_on_duplicate(self):
        Page.objects.create(slug="existing")
        page = Page(slug="existing")
        trigger = Page._meta.triggers[0]
        with pytest.raises(ValidationError) as exc_info:
            trigger.validate(Page, page)
        assert exc_info.value.code == "unique"

    def test_validate_passes_for_unique_value(self):
        Page.objects.create(slug="taken")
        page = Page(slug="available")
        trigger = Page._meta.triggers[0]
        trigger.validate(Page, page)

    def test_validate_skips_null(self):
        page = Page(slug=None)
        trigger = Page._meta.triggers[0]
        trigger.validate(Page, page)

    def test_validate_excludes_self_on_update(self):
        page = Page.objects.create(slug="mine")
        trigger = Page._meta.triggers[0]
        trigger.validate(Page, page)  # should not raise — it's the same row


# ---------------------------------------------------------------------------
# Trigger lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
class TestUniqueConstraintTriggerLifecycle:
    def _trigger_exists(self, name_fragment, table):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT 1 FROM pg_trigger t "
                "JOIN pg_class c ON c.oid = t.tgrelid "
                "WHERE t.tgname LIKE %s AND c.relname = %s",
                [f"%{name_fragment}%", table],
            )
            return cursor.fetchone() is not None

    def test_trigger_created(self):
        assert self._trigger_exists("page_unique_slug", "testapp_page")

    def test_remove_and_recreate(self):
        trigger = Page._meta.triggers[0]
        trigger.uninstall(Page)
        assert not self._trigger_exists("page_unique_slug", "testapp_page")

        trigger.install(Page)
        assert self._trigger_exists("page_unique_slug", "testapp_page")

    def test_install_is_idempotent(self):
        trigger = Page._meta.triggers[0]
        trigger.uninstall(Page)
        trigger.install(Page)
        trigger.install(Page)
        assert self._trigger_exists("page_unique_slug", "testapp_page")


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


def _raw_connect():
    db = settings.DATABASES["default"]
    return psycopg.connect(
        dbname=db["NAME"],
        user=db["USER"],
        password=db["PASSWORD"],
        host=db["HOST"],
        port=db["PORT"] or 5432,
        autocommit=False,
    )


@pytest.mark.django_db(transaction=True)
class TestUniqueConstraintTriggerConcurrency:
    def test_concurrent_insert_one_wins(self):
        """Advisory lock serialises concurrent inserts — exactly one must succeed.

        With immediate (non-deferred) triggers, the advisory lock is acquired
        during the INSERT itself.  Two threads racing to insert the same value
        will have one block on the lock until the other commits.
        """
        results: list[str | None] = [None, None]

        def do_insert(idx: int) -> None:
            conn = _raw_connect()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO testapp_page (slug, section) VALUES ('race-slug', 'main')",
                    )
                conn.commit()
                results[idx] = "ok"
            except Exception as e:  # noqa: BLE001
                results[idx] = type(e).__name__
                conn.rollback()
            finally:
                conn.close()

        threads = [
            threading.Thread(target=do_insert, args=(0,)),
            threading.Thread(target=do_insert, args=(1,)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        ok_count = results.count("ok")
        assert ok_count == 1, f"Expected exactly 1 success but got {results}"


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestUniqueConstraintTriggerConstruction:
    def test_empty_fields_and_expressions_raises(self):
        with pytest.raises(ValueError, match="At least one field"):
            UniqueConstraintTrigger(fields=[], name="c")

    def test_fields_stored(self):
        t = UniqueConstraintTrigger(fields=["slug", "section"], name="c")
        assert t.fields == ["slug", "section"]

    def test_expressions_stored(self):
        t = UniqueConstraintTrigger(Lower("slug"), name="c")
        assert len(t.expressions) == 1

    def test_fields_and_expressions_mutually_exclusive(self):
        with pytest.raises(ValueError, match="mutually exclusive"):
            UniqueConstraintTrigger(Lower("slug"), fields=["section"], name="c")

    def test_expressions_cannot_be_deferred(self):
        with pytest.raises(ValueError, match="cannot be deferred"):
            UniqueConstraintTrigger(
                Lower("slug"),
                deferrable=Deferrable.DEFERRED,
                name="c",
            )


@pytest.mark.django_db(transaction=True)
class TestUniqueConstraintTriggerExpressions:
    """Verify expression-based uniqueness (e.g. Lower)."""

    def test_case_insensitive_unique(self):
        """Lower("slug") should treat 'Hello' and 'hello' as duplicates."""
        trigger = UniqueConstraintTrigger(Lower("slug"), name="page_slug_ci")
        trigger.install(Page)
        try:
            Page.objects.create(slug="Hello")
            with pytest.raises(IntegrityError):
                Page.objects.create(slug="hello")
        finally:
            trigger.uninstall(Page)

    def test_case_insensitive_different_values_allowed(self):
        trigger = UniqueConstraintTrigger(Lower("slug"), name="page_slug_ci")
        trigger.install(Page)
        try:
            Page.objects.create(slug="alpha")
            Page.objects.create(slug="beta")
        finally:
            trigger.uninstall(Page)

    def test_parametrized_expression(self):
        """Left("slug", 3) — expressions with SQL parameters work correctly."""
        trigger = UniqueConstraintTrigger(Left("slug", 3), name="page_slug_prefix")
        trigger.install(Page)
        try:
            Page.objects.create(slug="abc-one")
            Page.objects.create(slug="abd-two")  # different prefix, allowed
            with pytest.raises(IntegrityError):
                Page.objects.create(slug="abc-three")  # same "abc" prefix
        finally:
            trigger.uninstall(Page)
