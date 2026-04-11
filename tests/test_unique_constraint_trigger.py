"""Tests for UniqueConstraintTrigger.

Covers: simple fields (immediate + deferred), FK-traversal (immediate + deferred),
multi-column, partial (condition), nulls_distinct, expressions, construction,
validation, lifecycle, and concurrency.
"""

import threading

import psycopg
import pytest
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Deferrable, Q
from django.db.models.functions import Left, Lower
from factories import (
    ChapterFactory,
    PageFactory,
    PublisherFactory,
    SeriesFactory,
)
from helpers import swap_trigger, trigger_exists
from testapp.models import Chapter, Page

from django_pgconstraints import UniqueConstraintTrigger

# ---------------------------------------------------------------------------
# Simple fields, non-deferred (default mode)
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_simple_duplicate_insert_blocked():
    PageFactory.create(slug="hello")
    with pytest.raises(IntegrityError):
        PageFactory.create(slug="hello")


@pytest.mark.django_db(transaction=True)
def test_simple_different_values_allowed():
    PageFactory.create(slug="alpha")
    PageFactory.create(slug="beta")
    PageFactory.create(slug="gamma")


@pytest.mark.django_db(transaction=True)
def test_simple_update_to_duplicate_blocked():
    PageFactory.create(slug="taken")
    page = PageFactory.create(slug="free")
    page.slug = "taken"
    with pytest.raises(IntegrityError):
        page.save()


@pytest.mark.django_db(transaction=True)
def test_simple_update_same_value_allowed():
    page = PageFactory.create(slug="mine")
    page.slug = "mine"
    page.save()


@pytest.mark.django_db(transaction=True)
def test_simple_update_unrelated_field_allowed():
    page = PageFactory.create(slug="hello")
    page.section = "updated"
    page.save()


@pytest.mark.django_db(transaction=True)
def test_simple_null_values_allowed():
    PageFactory.create(slug=None)
    PageFactory.create(slug=None)


@pytest.mark.django_db(transaction=True)
def test_simple_error_fires_immediately_not_at_commit():
    PageFactory.create(slug="taken")
    with pytest.raises(IntegrityError), transaction.atomic():
        PageFactory.create(slug="taken")
    PageFactory.create(slug="other")


# ---------------------------------------------------------------------------
# Simple fields, deferred (fires at COMMIT)
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_deferred_fires_at_commit():
    deferred = UniqueConstraintTrigger(
        fields=["slug"],
        deferrable=Deferrable.DEFERRED,
        name="page_slug_deferred",
    )
    with swap_trigger(Page, deferred):
        PageFactory.create(slug="existing")
        with pytest.raises(IntegrityError), transaction.atomic():
            PageFactory.create(slug="existing")


@pytest.mark.django_db(transaction=True)
def test_deferred_duplicate_insert_blocked():
    deferred = UniqueConstraintTrigger(
        fields=["slug"],
        deferrable=Deferrable.DEFERRED,
        name="page_slug_deferred",
    )
    with swap_trigger(Page, deferred):
        PageFactory.create(slug="dup")
        with pytest.raises(IntegrityError):
            PageFactory.create(slug="dup")


@pytest.mark.django_db(transaction=True)
def test_deferred_different_values_allowed():
    deferred = UniqueConstraintTrigger(
        fields=["slug"],
        deferrable=Deferrable.DEFERRED,
        name="page_slug_deferred",
    )
    with swap_trigger(Page, deferred):
        PageFactory.create(slug="one")
        PageFactory.create(slug="two")


@pytest.mark.django_db(transaction=True)
def test_deferred_temporary_dup_then_resolve_via_delete():
    """Deferred allows temporary duplicates if resolved before commit."""
    deferred = UniqueConstraintTrigger(
        fields=["slug"],
        deferrable=Deferrable.DEFERRED,
        name="page_slug_deferred",
    )
    with swap_trigger(Page, deferred):
        page = PageFactory.create(slug="target")
        with transaction.atomic():
            PageFactory.create(slug="target")
            page.delete()


@pytest.mark.django_db(transaction=True)
def test_deferred_two_inserts_same_tx_conflict():
    deferred = UniqueConstraintTrigger(
        fields=["slug"],
        deferrable=Deferrable.DEFERRED,
        name="page_slug_deferred",
    )
    with swap_trigger(Page, deferred), pytest.raises(IntegrityError), transaction.atomic():  # noqa: PT012
        PageFactory.create(slug="dup")
        PageFactory.create(slug="dup")


# ---------------------------------------------------------------------------
# FK-traversal, non-deferred (Chapter: name + series__publisher)
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_fk_same_name_different_publisher_allowed():
    pub_a = PublisherFactory.create()
    pub_b = PublisherFactory.create()
    series_a = SeriesFactory.create(publisher=pub_a)
    series_b = SeriesFactory.create(publisher=pub_b)
    ChapterFactory.create(name="Introduction", series=series_a)
    ChapterFactory.create(name="Introduction", series=series_b)


@pytest.mark.django_db(transaction=True)
def test_fk_same_name_same_publisher_different_series_blocked():
    pub = PublisherFactory.create()
    series_1 = SeriesFactory.create(publisher=pub)
    series_2 = SeriesFactory.create(publisher=pub)
    ChapterFactory.create(name="Introduction", series=series_1)
    with pytest.raises(IntegrityError):
        ChapterFactory.create(name="Introduction", series=series_2)


@pytest.mark.django_db(transaction=True)
def test_fk_same_name_same_series_blocked():
    series = SeriesFactory.create()
    ChapterFactory.create(name="Chapter 1", series=series)
    with pytest.raises(IntegrityError):
        ChapterFactory.create(name="Chapter 1", series=series)


@pytest.mark.django_db(transaction=True)
def test_fk_different_names_same_publisher_allowed():
    series = SeriesFactory.create()
    ChapterFactory.create(name="Chapter 1", series=series)
    ChapterFactory.create(name="Chapter 2", series=series)


@pytest.mark.django_db(transaction=True)
def test_fk_update_name_to_duplicate_blocked():
    series = SeriesFactory.create()
    ChapterFactory.create(name="Existing", series=series)
    chapter = ChapterFactory.create(name="Other", series=series)
    chapter.name = "Existing"
    with pytest.raises(IntegrityError):
        chapter.save()


@pytest.mark.django_db(transaction=True)
def test_fk_reassign_series_creates_conflict():
    pub_a = PublisherFactory.create()
    pub_b = PublisherFactory.create()
    series_a = SeriesFactory.create(publisher=pub_a)
    series_b = SeriesFactory.create(publisher=pub_b)
    ChapterFactory.create(name="Intro", series=series_a)
    chapter_b = ChapterFactory.create(name="Intro", series=series_b)
    series_a2 = SeriesFactory.create(publisher=pub_a)
    chapter_b.series = series_a2
    with pytest.raises(IntegrityError):
        chapter_b.save()


@pytest.mark.django_db(transaction=True)
def test_fk_multiple_series_same_publisher_unique_names():
    pub = PublisherFactory.create()
    series_list = [SeriesFactory.create(publisher=pub) for _ in range(5)]
    for i, s in enumerate(series_list):
        ChapterFactory.create(name=f"Chapter {i}", series=s)
    with pytest.raises(IntegrityError):
        ChapterFactory.create(name="Chapter 0", series=series_list[4])


# ---------------------------------------------------------------------------
# FK-traversal, deferred
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_fk_deferred_fires_at_commit():
    deferred = UniqueConstraintTrigger(
        fields=["name", "series__publisher"],
        deferrable=Deferrable.DEFERRED,
        name="chapter_name_pub_deferred",
    )
    with swap_trigger(Chapter, deferred):
        series = SeriesFactory.create()
        ChapterFactory.create(name="Ch", series=series)
        with pytest.raises(IntegrityError), transaction.atomic():
            ChapterFactory.create(name="Ch", series=series)


@pytest.mark.django_db(transaction=True)
def test_fk_deferred_different_publisher_allowed():
    deferred = UniqueConstraintTrigger(
        fields=["name", "series__publisher"],
        deferrable=Deferrable.DEFERRED,
        name="chapter_name_pub_deferred",
    )
    with swap_trigger(Chapter, deferred):
        pub_a = PublisherFactory.create()
        pub_b = PublisherFactory.create()
        series_a = SeriesFactory.create(publisher=pub_a)
        series_b = SeriesFactory.create(publisher=pub_b)
        ChapterFactory.create(name="Intro", series=series_a)
        ChapterFactory.create(name="Intro", series=series_b)


@pytest.mark.django_db(transaction=True)
def test_fk_deferred_checks_final_state():
    """Deferred trigger resolves FK chain at commit, not insert time."""
    deferred = UniqueConstraintTrigger(
        fields=["name", "series__publisher"],
        deferrable=Deferrable.DEFERRED,
        name="chapter_name_pub_deferred",
    )
    with swap_trigger(Chapter, deferred):
        pub_a = PublisherFactory.create()
        pub_b = PublisherFactory.create()
        s1 = SeriesFactory.create(publisher=pub_a)
        s2 = SeriesFactory.create(publisher=pub_b)
        ChapterFactory.create(name="Intro", series=s1)
        with pytest.raises(IntegrityError), transaction.atomic():  # noqa: PT012
            ChapterFactory.create(name="Intro", series=s2)
            s2.publisher = pub_a
            s2.save()


@pytest.mark.django_db(transaction=True)
def test_fk_deferred_temporary_dup_then_resolve_via_delete():
    deferred = UniqueConstraintTrigger(
        fields=["name", "series__publisher"],
        deferrable=Deferrable.DEFERRED,
        name="chapter_name_pub_deferred",
    )
    with swap_trigger(Chapter, deferred):
        series = SeriesFactory.create()
        ch = ChapterFactory.create(name="Intro", series=series)
        with transaction.atomic():
            ChapterFactory.create(name="Intro", series=series)
            ch.delete()


# ---------------------------------------------------------------------------
# Multi-column
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_multicolumn_same_slug_different_section_allowed():
    trigger = UniqueConstraintTrigger(
        fields=["slug", "section"],
        name="page_slug_section_unique",
    )
    with swap_trigger(Page, trigger):
        PageFactory.create(slug="hello", section="blog")
        PageFactory.create(slug="hello", section="docs")


@pytest.mark.django_db(transaction=True)
def test_multicolumn_same_slug_same_section_blocked():
    trigger = UniqueConstraintTrigger(
        fields=["slug", "section"],
        name="page_slug_section_unique",
    )
    with swap_trigger(Page, trigger):
        PageFactory.create(slug="hello", section="blog")
        with pytest.raises(IntegrityError):
            PageFactory.create(slug="hello", section="blog")


@pytest.mark.django_db(transaction=True)
def test_multicolumn_different_slug_same_section_allowed():
    trigger = UniqueConstraintTrigger(
        fields=["slug", "section"],
        name="page_slug_section_unique",
    )
    with swap_trigger(Page, trigger):
        PageFactory.create(slug="alpha", section="blog")
        PageFactory.create(slug="beta", section="blog")


# ---------------------------------------------------------------------------
# Partial uniqueness via condition
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_condition_duplicate_in_published_blocked():
    trigger = UniqueConstraintTrigger(
        fields=["slug"],
        condition=Q(section="published"),
        name="page_slug_unique_when_published",
    )
    with swap_trigger(Page, trigger):
        PageFactory.create(slug="hello", section="published")
        with pytest.raises(IntegrityError):
            PageFactory.create(slug="hello", section="published")


@pytest.mark.django_db(transaction=True)
def test_condition_duplicate_in_draft_allowed():
    trigger = UniqueConstraintTrigger(
        fields=["slug"],
        condition=Q(section="published"),
        name="page_slug_unique_when_published",
    )
    with swap_trigger(Page, trigger):
        PageFactory.create(slug="hello", section="draft")
        PageFactory.create(slug="hello", section="draft")


@pytest.mark.django_db(transaction=True)
def test_condition_duplicate_across_sections_allowed():
    trigger = UniqueConstraintTrigger(
        fields=["slug"],
        condition=Q(section="published"),
        name="page_slug_unique_when_published",
    )
    with swap_trigger(Page, trigger):
        PageFactory.create(slug="hello", section="published")
        PageFactory.create(slug="hello", section="draft")


# ---------------------------------------------------------------------------
# nulls_distinct=False
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_nulls_not_distinct_two_nulls_blocked():
    trigger = UniqueConstraintTrigger(
        fields=["slug"],
        nulls_distinct=False,
        name="page_slug_nulls_not_distinct",
    )
    with swap_trigger(Page, trigger):
        PageFactory.create(slug=None)
        with pytest.raises(IntegrityError):
            PageFactory.create(slug=None)


@pytest.mark.django_db(transaction=True)
def test_nulls_not_distinct_null_and_value_allowed():
    trigger = UniqueConstraintTrigger(
        fields=["slug"],
        nulls_distinct=False,
        name="page_slug_nulls_not_distinct",
    )
    with swap_trigger(Page, trigger):
        PageFactory.create(slug=None)
        PageFactory.create(slug="hello")


@pytest.mark.django_db(transaction=True)
def test_nulls_not_distinct_duplicate_non_null_blocked():
    trigger = UniqueConstraintTrigger(
        fields=["slug"],
        nulls_distinct=False,
        name="page_slug_nulls_not_distinct",
    )
    with swap_trigger(Page, trigger):
        PageFactory.create(slug="hello")
        with pytest.raises(IntegrityError):
            PageFactory.create(slug="hello")


# ---------------------------------------------------------------------------
# Expression-based uniqueness
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_expression_lower_blocks_case_insensitive_duplicate():
    trigger = UniqueConstraintTrigger(Lower("slug"), name="page_slug_lower")
    with swap_trigger(Page, trigger):
        PageFactory.create(slug="Hello")
        with pytest.raises(IntegrityError):
            PageFactory.create(slug="hello")


@pytest.mark.django_db(transaction=True)
def test_expression_lower_allows_different_values():
    trigger = UniqueConstraintTrigger(Lower("slug"), name="page_slug_lower")
    with swap_trigger(Page, trigger):
        PageFactory.create(slug="alpha")
        PageFactory.create(slug="beta")


@pytest.mark.django_db(transaction=True)
def test_expression_lower_update_to_case_insensitive_duplicate_blocked():
    trigger = UniqueConstraintTrigger(Lower("slug"), name="page_slug_lower")
    with swap_trigger(Page, trigger):
        PageFactory.create(slug="Hello")
        page = PageFactory.create(slug="World")
        page.slug = "HELLO"
        with pytest.raises(IntegrityError):
            page.save()


@pytest.mark.django_db(transaction=True)
def test_expression_parametrized_left_prefix():
    """Left("slug", 3) — expressions with SQL parameters."""
    trigger = UniqueConstraintTrigger(Left("slug", 3), name="page_slug_prefix")
    with swap_trigger(Page, trigger):
        PageFactory.create(slug="abc-one")
        PageFactory.create(slug="abd-two")
        with pytest.raises(IntegrityError):
            PageFactory.create(slug="abc-three")


# ---------------------------------------------------------------------------
# Python-level validation (validate())
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_validate_raises_on_duplicate():
    PageFactory.create(slug="existing")
    page = Page(slug="existing")
    trigger = Page._meta.triggers[0]
    with pytest.raises(ValidationError) as exc_info:
        trigger.validate(Page, page)
    assert exc_info.value.code == "unique"


@pytest.mark.django_db(transaction=True)
def test_validate_passes_for_unique_value():
    PageFactory.create(slug="taken")
    page = Page(slug="available")
    trigger = Page._meta.triggers[0]
    trigger.validate(Page, page)


@pytest.mark.django_db(transaction=True)
def test_validate_skips_null():
    page = Page(slug=None)
    trigger = Page._meta.triggers[0]
    trigger.validate(Page, page)


@pytest.mark.django_db(transaction=True)
def test_validate_excludes_self_on_update():
    page = PageFactory.create(slug="mine")
    trigger = Page._meta.triggers[0]
    trigger.validate(Page, page)


# ---------------------------------------------------------------------------
# Construction (pure Python, no DB)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_construction_empty_fields_and_expressions_raises():
    with pytest.raises(ValueError, match="At least one field"):
        UniqueConstraintTrigger(fields=[], name="c")


@pytest.mark.unit
def test_construction_fields_stored():
    t = UniqueConstraintTrigger(fields=["slug", "section"], name="c")
    assert t.fields == ["slug", "section"]


@pytest.mark.unit
def test_construction_expressions_stored():
    t = UniqueConstraintTrigger(Lower("slug"), name="c")
    assert len(t.expressions) == 1


@pytest.mark.unit
def test_construction_fields_and_expressions_mutually_exclusive():
    with pytest.raises(ValueError, match="mutually exclusive"):
        UniqueConstraintTrigger(Lower("slug"), fields=["section"], name="c")


@pytest.mark.unit
def test_construction_expressions_cannot_be_deferred():
    with pytest.raises(ValueError, match="cannot be deferred"):
        UniqueConstraintTrigger(
            Lower("slug"),
            deferrable=Deferrable.DEFERRED,
            name="c",
        )


# ---------------------------------------------------------------------------
# Lifecycle (install / uninstall / idempotence)
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_lifecycle_trigger_created():
    assert trigger_exists("page_unique_slug", "testapp_page")


@pytest.mark.django_db(transaction=True)
def test_lifecycle_remove_and_recreate():
    trigger = Page._meta.triggers[0]
    trigger.uninstall(Page)
    assert not trigger_exists("page_unique_slug", "testapp_page")
    trigger.install(Page)
    assert trigger_exists("page_unique_slug", "testapp_page")


@pytest.mark.django_db(transaction=True)
def test_lifecycle_install_is_idempotent():
    trigger = Page._meta.triggers[0]
    trigger.uninstall(Page)
    trigger.install(Page)
    trigger.install(Page)
    assert trigger_exists("page_unique_slug", "testapp_page")


# ---------------------------------------------------------------------------
# Concurrency — advisory lock serialises racing inserts
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
def test_concurrent_insert_exactly_one_wins():
    """With immediate triggers, advisory lock serialises the race."""
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

    assert results.count("ok") == 1, f"Expected exactly 1 success but got {results}"
