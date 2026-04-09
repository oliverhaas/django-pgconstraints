"""Tests for MaintainedCount constraint."""

import pytest
from django.db import connection
from testapp.models import Author, Book

from django_pgconstraints import MaintainedCount

# ---------------------------------------------------------------------------
# DB-level enforcement
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
class TestMaintainedCountEnforcement:
    def test_insert_increments(self):
        author = Author.objects.create(name="Alice")
        Book.objects.create(title="Book A", author=author)
        author.refresh_from_db()
        assert author.book_count == 1

    def test_multiple_inserts(self):
        author = Author.objects.create(name="Alice")
        Book.objects.create(title="Book A", author=author)
        Book.objects.create(title="Book B", author=author)
        Book.objects.create(title="Book C", author=author)
        author.refresh_from_db()
        assert author.book_count == 3

    def test_delete_decrements(self):
        author = Author.objects.create(name="Alice")
        book = Book.objects.create(title="Book A", author=author)
        book.delete()
        author.refresh_from_db()
        assert author.book_count == 0

    def test_queryset_delete(self):
        author = Author.objects.create(name="Alice")
        Book.objects.create(title="Book A", author=author)
        Book.objects.create(title="Book B", author=author)
        Book.objects.filter(author=author).delete()
        author.refresh_from_db()
        assert author.book_count == 0

    def test_fk_update_adjusts_both(self):
        alice = Author.objects.create(name="Alice")
        bob = Author.objects.create(name="Bob")
        book = Book.objects.create(title="Book A", author=alice)
        alice.refresh_from_db()
        assert alice.book_count == 1

        book.author = bob
        book.save()
        alice.refresh_from_db()
        bob.refresh_from_db()
        assert alice.book_count == 0
        assert bob.book_count == 1

    def test_multiple_authors(self):
        alice = Author.objects.create(name="Alice")
        bob = Author.objects.create(name="Bob")
        Book.objects.create(title="A1", author=alice)
        Book.objects.create(title="A2", author=alice)
        Book.objects.create(title="B1", author=bob)
        alice.refresh_from_db()
        bob.refresh_from_db()
        assert alice.book_count == 2
        assert bob.book_count == 1

    def test_bulk_create(self):
        author = Author.objects.create(name="Alice")
        Book.objects.bulk_create(
            [
                Book(title="B1", author=author),
                Book(title="B2", author=author),
                Book(title="B3", author=author),
            ]
        )
        author.refresh_from_db()
        assert author.book_count == 3


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------


class TestMaintainedCountDeconstruct:
    def test_deconstruct(self):
        constraint = MaintainedCount(
            target="myapp.Post",
            target_field="comment_count",
            fk_field="post",
            name="c",
        )
        path, args, kwargs = constraint.deconstruct()
        assert path == "django_pgconstraints.MaintainedCount"
        assert args == ()
        assert kwargs["target"] == "myapp.Post"
        assert kwargs["target_field"] == "comment_count"
        assert kwargs["fk_field"] == "post"

    def test_roundtrip(self):
        original = MaintainedCount(
            target="myapp.Post",
            target_field="comment_count",
            fk_field="post",
            name="c",
        )
        _, args, kwargs = original.deconstruct()
        restored = MaintainedCount(*args, **kwargs)
        assert original == restored


# ---------------------------------------------------------------------------
# Trigger lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
class TestMaintainedCountLifecycle:
    def _trigger_exists(self, name, table):
        with connection.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM information_schema.triggers WHERE trigger_name = %s AND event_object_table = %s",
                [name, table],
            )
            return cur.fetchone() is not None

    def test_triggers_created(self):
        base = "testapp_maintain_author_book_count"
        table = "testapp_book"
        assert self._trigger_exists(f"{base}_ins", table)
        assert self._trigger_exists(f"{base}_del", table)
        assert self._trigger_exists(f"{base}_upd", table)

    def test_remove_and_recreate(self):
        constraint = Book._meta.constraints[0]
        base = "testapp_maintain_author_book_count"
        table = "testapp_book"

        with connection.schema_editor() as editor:
            editor.remove_constraint(Book, constraint)
        assert not self._trigger_exists(f"{base}_ins", table)
        assert not self._trigger_exists(f"{base}_del", table)
        assert not self._trigger_exists(f"{base}_upd", table)

        with connection.schema_editor() as editor:
            editor.add_constraint(Book, constraint)
        assert self._trigger_exists(f"{base}_ins", table)
        assert self._trigger_exists(f"{base}_del", table)
        assert self._trigger_exists(f"{base}_upd", table)
