"""Tests for MaintainedCount constraint."""

import pytest
from django.db import connection
from testapp.models import Author, Book

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
            ],
        )
        author.refresh_from_db()
        assert author.book_count == 3


# ---------------------------------------------------------------------------
# Trigger lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
class TestMaintainedCountLifecycle:
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
        table = "testapp_book"
        assert self._trigger_exists("maintain_author_book_count_ins", table)
        assert self._trigger_exists("maintain_author_book_count_del", table)
        assert self._trigger_exists("maintain_author_book_count_upd", table)

    def test_remove_and_recreate(self):
        triggers = Book._meta.triggers
        table = "testapp_book"

        for trigger in triggers:
            trigger.uninstall(Book)
        assert not self._trigger_exists("maintain_author_book_count_ins", table)
        assert not self._trigger_exists("maintain_author_book_count_del", table)
        assert not self._trigger_exists("maintain_author_book_count_upd", table)

        for trigger in triggers:
            trigger.install(Book)
        assert self._trigger_exists("maintain_author_book_count_ins", table)
        assert self._trigger_exists("maintain_author_book_count_del", table)
        assert self._trigger_exists("maintain_author_book_count_upd", table)
