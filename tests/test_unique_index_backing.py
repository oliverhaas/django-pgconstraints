"""Tests for UniqueConstraintTrigger(index=True) — issue #10.

Covers init-time validation (ValueError for non-indexable configs),
install/uninstall lifecycle, pg_indexes introspection, and end-to-end
duplicate rejection at the index level.
"""

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


# ---------------------------------------------------------------------------
# Composite unique index
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_composite_index_is_installed():
    with connection.cursor() as cur:
        cur.execute(
            "SELECT indexdef FROM pg_indexes "
            "WHERE tablename = 'testapp_indexedcompositepage' "
            "AND indexname LIKE 'pgconstraints_idx_%%'",
        )
        rows = cur.fetchall()
    assert rows
    idxdef = rows[0][0]
    assert "UNIQUE" in idxdef
    # pg_indexes normalizes unquoted identifiers, so no double-quotes here.
    assert "slug" in idxdef
    assert "section" in idxdef


@pytest.mark.django_db(transaction=True)
def test_composite_index_allows_same_slug_different_section():
    from testapp.models import IndexedCompositePage  # noqa: PLC0415

    IndexedCompositePage.objects.create(slug="dup", section="a")
    # Same slug, different section — allowed by composite uniqueness.
    IndexedCompositePage.objects.create(slug="dup", section="b")


@pytest.mark.django_db(transaction=True)
def test_composite_index_rejects_same_slug_same_section():
    from testapp.models import IndexedCompositePage  # noqa: PLC0415

    IndexedCompositePage.objects.create(slug="dup", section="a")
    with pytest.raises(IntegrityError):
        IndexedCompositePage.objects.create(slug="dup", section="a")


# ---------------------------------------------------------------------------
# Functional (expression) unique index — Lower("slug")
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_lower_index_is_installed():
    with connection.cursor() as cur:
        cur.execute(
            "SELECT indexdef FROM pg_indexes "
            "WHERE tablename = 'testapp_indexedlowerpage' "
            "AND indexname LIKE 'pgconstraints_idx_%%'",
        )
        rows = cur.fetchall()
    assert rows
    idxdef = rows[0][0]
    assert "UNIQUE" in idxdef
    assert "lower" in idxdef.lower()


@pytest.mark.django_db(transaction=True)
def test_lower_index_treats_case_variants_as_duplicate():
    from testapp.models import IndexedLowerPage  # noqa: PLC0415

    IndexedLowerPage.objects.create(slug="Hello")
    with pytest.raises(IntegrityError):
        IndexedLowerPage.objects.create(slug="HELLO")  # same LOWER() value


# ---------------------------------------------------------------------------
# NULLS NOT DISTINCT unique index (PG 15+)
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_nulls_not_distinct_index_is_installed():
    with connection.cursor() as cur:
        cur.execute(
            "SELECT indexdef FROM pg_indexes "
            "WHERE tablename = 'testapp_indexednullsnotdistinctpage' "
            "AND indexname LIKE 'pgconstraints_idx_%%'",
        )
        rows = cur.fetchall()
    assert rows
    idxdef = rows[0][0]
    assert "UNIQUE" in idxdef
    assert "NULLS NOT DISTINCT" in idxdef


@pytest.mark.django_db(transaction=True)
def test_nulls_not_distinct_index_rejects_duplicate_nulls():
    from testapp.models import IndexedNullsNotDistinctPage  # noqa: PLC0415

    IndexedNullsNotDistinctPage.objects.create(slug=None)
    with pytest.raises(IntegrityError):
        IndexedNullsNotDistinctPage.objects.create(slug=None)


# ---------------------------------------------------------------------------
# Partial unique index (condition=Q(...))
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_partial_index_is_installed_with_where_clause():
    with connection.cursor() as cur:
        cur.execute(
            "SELECT indexdef FROM pg_indexes "
            "WHERE tablename = 'testapp_indexedpartialpage' "
            "AND indexname LIKE 'pgconstraints_idx_%%'",
        )
        rows = cur.fetchall()
    assert rows
    idxdef = rows[0][0]
    assert "UNIQUE" in idxdef
    assert "WHERE" in idxdef.upper()
    assert "published" in idxdef.lower()


@pytest.mark.django_db(transaction=True)
def test_partial_index_rejects_duplicate_when_condition_met():
    from testapp.models import IndexedPartialPage  # noqa: PLC0415

    IndexedPartialPage.objects.create(slug="dup", published=True)
    with pytest.raises(IntegrityError):
        IndexedPartialPage.objects.create(slug="dup", published=True)


@pytest.mark.django_db(transaction=True)
def test_partial_index_allows_duplicate_when_condition_not_met():
    from testapp.models import IndexedPartialPage  # noqa: PLC0415

    IndexedPartialPage.objects.create(slug="dup", published=False)
    # Partial index: rows with published=False are outside the index,
    # so duplicates among unpublished rows are allowed.
    IndexedPartialPage.objects.create(slug="dup", published=False)
