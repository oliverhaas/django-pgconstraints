"""Tests for UniqueConstraintTrigger(index=True) — issue #10.

Covers init-time validation (ValueError for non-indexable configs),
install/uninstall lifecycle, pg_indexes introspection, and end-to-end
duplicate rejection at the index level.
"""

from __future__ import annotations

import pytest
from django.db import IntegrityError, connection
from django.db.models import Deferrable, F

from django_pgconstraints import UniqueConstraintTrigger

# ---------------------------------------------------------------------------
# Init-time validation: non-indexable configurations must raise ValueError
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_index_rejects_fk_traversal_in_fields():
    """`fields` containing a '__' FK chain cannot be backed by an index."""
    with pytest.raises(ValueError, match="FK traversal"):
        UniqueConstraintTrigger(
            fields=["name", "series__publisher"],
            index=True,
            name="fk_chain_index",
        )


@pytest.mark.unit
def test_index_rejects_deferred_constraint():
    """Unique indexes cannot be deferred; DEFERRED + index=True is contradictory."""
    with pytest.raises(ValueError, match="deferred"):
        UniqueConstraintTrigger(
            fields=["slug"],
            index=True,
            deferrable=Deferrable.DEFERRED,
            name="deferred_index",
        )


@pytest.mark.unit
def test_index_rejects_fk_traversal_in_expression():
    """An F() reference with '__' FK chain in an expression is not indexable."""
    with pytest.raises(ValueError, match="FK traversal"):
        UniqueConstraintTrigger(
            F("book__author"),
            index=True,
            name="expr_fk_chain_index",
        )


@pytest.mark.unit
def test_no_index_kwarg_still_accepts_fk_traversal():
    """Without index=True, FK traversal is fine — the trigger-only path."""
    # Must not raise.
    UniqueConstraintTrigger(
        fields=["name", "series__publisher"],
        name="fk_chain_trigger_only",
    )


# ---------------------------------------------------------------------------
# Install/uninstall lifecycle + pg_indexes introspection
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_plain_index_is_installed_in_pg_catalog():
    """After migrations run, a `CREATE UNIQUE INDEX` must exist in
    pg_indexes for IndexedSlugPage's slug field."""
    with connection.cursor() as cur:
        cur.execute(
            "SELECT indexdef FROM pg_indexes "
            "WHERE tablename = 'testapp_indexedslugpage' "
            "AND indexname LIKE 'pgconstraints_idx_%%'",
        )
        rows = cur.fetchall()
    assert rows, "no pgconstraints_idx_* index found on testapp_indexedslugpage"
    defs = [r[0] for r in rows]
    assert any("UNIQUE" in d and "slug" in d for d in defs), f"expected a UNIQUE INDEX on slug; got: {defs}"


@pytest.mark.django_db(transaction=True)
def test_plain_index_rejects_duplicate_insert():
    """End-to-end: duplicate insert must raise IntegrityError with code 23505
    from the index (not the trigger's RAISE)."""
    from testapp.models import IndexedSlugPage  # noqa: PLC0415

    IndexedSlugPage.objects.create(slug="taken")

    with pytest.raises(IntegrityError) as exc_info:
        IndexedSlugPage.objects.create(slug="taken")

    # PG error code 23505 = unique_violation. Both trigger-level and
    # index-level rejections use this code, so we can't distinguish on
    # the code alone — but index-level rejections name the index in the
    # error detail, while trigger-level ones name the trigger.
    assert "pgconstraints_idx_" in str(exc_info.value), (
        f"Expected index-level rejection (message mentioning the index name), "
        f"got: {exc_info.value}. If this message mentions the trigger name, "
        f"the index isn't being installed, or the trigger is rejecting first."
    )


@pytest.mark.django_db(transaction=True)
def test_explicit_install_uninstall_roundtrip():
    """The `install()` / `uninstall()` methods (invoked by `manage.py
    pgtrigger install`) must also create/drop the backing index, not just
    the post_migrate path."""
    from testapp.models import IndexedSlugPage  # noqa: PLC0415

    trigger = IndexedSlugPage._meta.triggers[0]
    assert isinstance(trigger, UniqueConstraintTrigger)
    assert trigger.index is True

    # Drop the existing index (installed by post_migrate at test setup).
    trigger.uninstall(IndexedSlugPage)  # type: ignore[arg-type]

    with connection.cursor() as cur:
        cur.execute(
            "SELECT indexname FROM pg_indexes "
            "WHERE tablename = 'testapp_indexedslugpage' "
            "AND indexname LIKE 'pgconstraints_idx_%%'",
        )
        assert cur.fetchall() == [], "uninstall() did not drop the index"

    # Explicit install() should reinstall it.
    trigger.install(IndexedSlugPage)  # type: ignore[arg-type]

    with connection.cursor() as cur:
        cur.execute(
            "SELECT indexname FROM pg_indexes "
            "WHERE tablename = 'testapp_indexedslugpage' "
            "AND indexname LIKE 'pgconstraints_idx_%%'",
        )
        rows = cur.fetchall()
    assert rows, "install() did not create the index"
