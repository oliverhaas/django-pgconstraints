"""Tests for UniqueConstraintTrigger."""

import threading

import psycopg
import pytest
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from testapp.models import Page, Post

from django_pgconstraints import UniqueConstraintTrigger

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
    """Verify the Python-side validate() method."""

    def test_validate_raises_on_duplicate(self):
        Page.objects.create(slug="existing")
        post = Post(slug="existing")
        constraint = Post._meta.constraints[0]
        with pytest.raises(ValidationError) as exc_info:
            constraint.validate(Post, post)
        assert exc_info.value.code == "cross_table_unique"

    def test_validate_passes_for_unique_value(self):
        Page.objects.create(slug="taken")
        post = Post(slug="available")
        constraint = Post._meta.constraints[0]
        constraint.validate(Post, post)  # should not raise

    def test_validate_skips_excluded_field(self):
        Page.objects.create(slug="existing")
        post = Post(slug="existing")
        constraint = Post._meta.constraints[0]
        constraint.validate(Post, post, exclude={"slug"})  # should not raise

    def test_validate_skips_null(self):
        post = Post(slug=None)
        constraint = Post._meta.constraints[0]
        constraint.validate(Post, post)  # should not raise


# ---------------------------------------------------------------------------
# Migration serialisation
# ---------------------------------------------------------------------------


class TestUniqueConstraintTriggerDeconstruct:
    """Verify deconstruct() produces a serialisable representation."""

    def test_deconstruct_basic(self):
        constraint = UniqueConstraintTrigger(field="slug", across="myapp.Post", name="my_constraint")
        path, args, kwargs = constraint.deconstruct()
        assert path == "django_pgconstraints.UniqueConstraintTrigger"
        assert args == ()
        assert kwargs["field"] == "slug"
        assert kwargs["across"] == "myapp.Post"
        assert kwargs["name"] == "my_constraint"
        assert "across_field" not in kwargs

    def test_deconstruct_with_across_field(self):
        constraint = UniqueConstraintTrigger(
            field="slug",
            across="myapp.Post",
            across_field="url_slug",
            name="my_constraint",
        )
        _, _, kwargs = constraint.deconstruct()
        assert kwargs["across_field"] == "url_slug"

    def test_deconstruct_omits_across_field_when_same(self):
        constraint = UniqueConstraintTrigger(field="slug", across="myapp.Post", name="c")
        _, _, kwargs = constraint.deconstruct()
        assert "across_field" not in kwargs

    def test_roundtrip(self):
        original = UniqueConstraintTrigger(
            field="slug",
            across="myapp.Post",
            across_field="url_slug",
            name="my_constraint",
        )
        path, args, kwargs = original.deconstruct()
        restored = UniqueConstraintTrigger(*args, **kwargs)
        assert original == restored


# ---------------------------------------------------------------------------
# Equality / hashing
# ---------------------------------------------------------------------------


class TestUniqueConstraintTriggerEquality:
    def test_equal(self):
        a = UniqueConstraintTrigger(field="slug", across="myapp.Post", name="c")
        b = UniqueConstraintTrigger(field="slug", across="myapp.Post", name="c")
        assert a == b

    def test_not_equal_different_field(self):
        a = UniqueConstraintTrigger(field="slug", across="myapp.Post", name="c")
        b = UniqueConstraintTrigger(field="title", across="myapp.Post", name="c")
        assert a != b

    def test_hashable(self):
        c = UniqueConstraintTrigger(field="slug", across="myapp.Post", name="c")
        assert hash(c) == hash(c)
        assert {c}  # can be added to a set


# ---------------------------------------------------------------------------
# Trigger lifecycle (create / remove SQL)
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
class TestUniqueConstraintTriggerTriggerLifecycle:
    """Verify triggers and functions can be created and removed cleanly."""

    def _trigger_exists(self, trigger_name, table_name):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT 1 FROM information_schema.triggers WHERE trigger_name = %s AND event_object_table = %s",
                [trigger_name, table_name],
            )
            return cursor.fetchone() is not None

    def _function_exists(self, function_name):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT 1 FROM pg_proc WHERE proname = %s",
                [function_name],
            )
            return cursor.fetchone() is not None

    def test_triggers_created(self):
        assert self._trigger_exists("testapp_page_unique_slug_across_post", "testapp_page")
        assert self._trigger_exists("testapp_post_unique_slug_across_page", "testapp_post")

    def test_functions_created(self):
        assert self._function_exists("pgc_fn_testapp_page_testapp_page_unique_slug_across_post")
        assert self._function_exists("pgc_fn_testapp_post_testapp_post_unique_slug_across_page")

    def test_remove_and_recreate(self):
        constraint = Page._meta.constraints[0]
        fn_name = "pgc_fn_testapp_page_testapp_page_unique_slug_across_post"
        with connection.schema_editor() as editor:
            editor.remove_constraint(Page, constraint)

        assert not self._trigger_exists("testapp_page_unique_slug_across_post", "testapp_page")
        assert not self._function_exists(fn_name)

        with connection.schema_editor() as editor:
            editor.add_constraint(Page, constraint)

        assert self._trigger_exists("testapp_page_unique_slug_across_post", "testapp_page")
        assert self._function_exists(fn_name)

    def test_create_function_is_idempotent(self):
        """CREATE OR REPLACE FUNCTION allows re-running without error."""
        constraint = Page._meta.constraints[0]
        with connection.schema_editor() as editor:
            # Calling create_sql when the function already exists should not
            # fail thanks to CREATE OR REPLACE.  Drop only the trigger so the
            # second create_sql can re-create it.
            editor.execute(
                f"DROP TRIGGER IF EXISTS {editor.quote_name(constraint.name)} ON {editor.quote_name('testapp_page')}",
            )
            # This re-creates both function (OR REPLACE) and trigger.
            editor.execute(constraint.create_sql(Page, editor))


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
