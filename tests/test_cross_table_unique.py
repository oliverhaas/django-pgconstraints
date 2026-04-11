"""Tests for cross-table unique constraint (pgtrigger-based)."""

import threading

import psycopg
import pytest
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from testapp.models import Page, Post

from django_pgconstraints import validate_unique_across

# ---------------------------------------------------------------------------
# DB-level enforcement (requires real transactions so deferred triggers fire)
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
class TestUniqueConstraintTriggerEnforcement:
    """Verify the PostgreSQL trigger blocks duplicate values across tables."""

    def test_insert_duplicate_across_tables(self):
        Page.objects.create(slug="hello")
        with pytest.raises(IntegrityError):
            Post.objects.create(slug="hello")

    def test_reverse_direction(self):
        Post.objects.create(slug="world")
        with pytest.raises(IntegrityError):
            Page.objects.create(slug="world")

    def test_different_values_allowed(self):
        Page.objects.create(slug="page-slug")
        Post.objects.create(slug="post-slug")

    def test_distinct_values_in_same_pair(self):
        """Multiple distinct values across the pair should all coexist."""
        Page.objects.create(slug="alpha")
        Post.objects.create(slug="beta")
        Page.objects.create(slug="gamma")
        Post.objects.create(slug="delta")

    def test_update_to_duplicate_blocked(self):
        Page.objects.create(slug="taken")
        post = Post.objects.create(slug="free")
        post.slug = "taken"
        with pytest.raises(IntegrityError):
            post.save()

    def test_same_value_in_same_table_uses_regular_unique(self):
        """Within-table uniqueness is handled by the regular unique constraint."""
        Page.objects.create(slug="dup")
        with pytest.raises(IntegrityError):
            Page.objects.create(slug="dup")

    def test_deferred_trigger_fires_at_commit(self):
        """The constraint trigger is INITIALLY DEFERRED — it fires at commit, not at statement time."""
        Page.objects.create(slug="deferred-test")
        with pytest.raises(IntegrityError), transaction.atomic():
            # Inside the transaction, the INSERT succeeds (trigger is deferred).
            Post.objects.create(slug="deferred-test")
            # Trigger fires when atomic() commits — IntegrityError propagates.


# ---------------------------------------------------------------------------
# Python-level validation
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
class TestUniqueConstraintTriggerValidation:
    """Verify the standalone validate_unique_across() helper."""

    def test_validate_raises_on_duplicate(self):
        Page.objects.create(slug="existing")
        post = Post(slug="existing")
        with pytest.raises(ValidationError) as exc_info:
            validate_unique_across(instance=post, field="slug", across="testapp.Page")
        assert exc_info.value.code == "cross_table_unique"

    def test_validate_passes_for_unique_value(self):
        Page.objects.create(slug="taken")
        post = Post(slug="available")
        validate_unique_across(instance=post, field="slug", across="testapp.Page")  # should not raise

    def test_validate_skips_null(self):
        post = Post(slug=None)
        validate_unique_across(instance=post, field="slug", across="testapp.Page")  # should not raise


# ---------------------------------------------------------------------------
# Trigger lifecycle (install / uninstall)
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
class TestUniqueConstraintTriggerTriggerLifecycle:
    """Verify triggers can be installed and uninstalled cleanly."""

    def _trigger_exists(self, name_fragment, table):
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT 1
                  FROM pg_trigger t
                  JOIN pg_class c ON c.oid = t.tgrelid
                 WHERE t.tgname LIKE %s
                   AND c.relname = %s
                """,
                [f"%{name_fragment}%", table],
            )
            return cursor.fetchone() is not None

    def test_triggers_created(self):
        assert self._trigger_exists("page_unique_slug_across_post", "testapp_page")
        assert self._trigger_exists("post_unique_slug_across_page", "testapp_post")

    def test_remove_and_recreate(self):
        trigger = Page._meta.triggers[0]

        trigger.uninstall(Page)
        assert not self._trigger_exists("page_unique_slug_across_post", "testapp_page")

        trigger.install(Page)
        assert self._trigger_exists("page_unique_slug_across_post", "testapp_page")

    def test_create_function_is_idempotent(self):
        """Uninstall then install twice — pgtrigger is idempotent."""
        trigger = Page._meta.triggers[0]

        trigger.uninstall(Page)
        trigger.install(Page)
        trigger.install(Page)  # second install should not fail

        assert self._trigger_exists("page_unique_slug_across_post", "testapp_page")


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


def _raw_connect():
    """Open a raw psycopg connection to the test database."""
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
    """Verify that concurrent inserts of the same value into two tables
    result in exactly one success — the loser gets an IntegrityError."""

    def test_concurrent_cross_table_insert(self):
        """Two threads insert the same slug into Page and Post concurrently.

        A threading.Barrier synchronises so both have INSERTed before
        either COMMITs.  The advisory lock in the trigger serialises the
        commits — exactly one must succeed, the other gets IntegrityError.
        """
        results: list[str | None] = [None, None]
        barrier = threading.Barrier(2, timeout=5)

        def do_insert(table: str, idx: int) -> None:
            conn = _raw_connect()
            try:
                with conn.cursor() as cur:
                    cur.execute(f"INSERT INTO testapp_{table} (slug) VALUES ('race-slug')")
                barrier.wait()  # both have inserted, now commit ~simultaneously
                conn.commit()
                results[idx] = "ok"
            except Exception as e:  # noqa: BLE001
                results[idx] = type(e).__name__
                conn.rollback()
            finally:
                conn.close()

        threads = [
            threading.Thread(target=do_insert, args=("page", 0)),
            threading.Thread(target=do_insert, args=("post", 1)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        ok_count = results.count("ok")
        assert ok_count == 1, f"Expected exactly 1 success but got {results}"
