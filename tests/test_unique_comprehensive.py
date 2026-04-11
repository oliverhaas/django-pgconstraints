"""Comprehensive UniqueConstraintTrigger tests covering all modes.

Test matrix:
- Simple fields, non-deferred (default)
- Simple fields, deferred
- FK-traversal fields, non-deferred
- FK-traversal fields, deferred
- Multi-column, non-deferred
- Condition (partial unique)
- Expressions (Lower, Left)
- nulls_distinct=False
"""

import pytest
from django.db import IntegrityError, transaction
from django.db.models import Deferrable
from django.db.models.functions import Lower
from testapp.models import Chapter, Page, Publisher, Series

from django_pgconstraints import UniqueConstraintTrigger

# ---------------------------------------------------------------------------
# Simple fields, non-deferred (fires immediately on INSERT/UPDATE)
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
class TestSimpleNonDeferred:
    """Default mode: fires immediately, single-table, plain fields."""

    def test_duplicate_insert_blocked(self):
        Page.objects.create(slug="hello")
        with pytest.raises(IntegrityError):
            Page.objects.create(slug="hello")

    def test_update_to_duplicate_blocked(self):
        Page.objects.create(slug="taken")
        page = Page.objects.create(slug="free")
        page.slug = "taken"
        with pytest.raises(IntegrityError):
            page.save()

    def test_different_values_coexist(self):
        Page.objects.create(slug="alpha")
        Page.objects.create(slug="beta")
        Page.objects.create(slug="gamma")

    def test_update_same_value_allowed(self):
        page = Page.objects.create(slug="mine")
        page.section = "other"  # change unrelated field
        page.save()

    def test_null_values_skip(self):
        """NULLs are distinct by default — multiple NULLs don't conflict."""
        Page.objects.create(slug=None)
        Page.objects.create(slug=None)


# ---------------------------------------------------------------------------
# Simple fields, deferred (fires at COMMIT)
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
class TestSimpleDeferred:
    """Deferred mode: fires at commit, not at statement time."""

    def _install_deferred(self):
        trigger = UniqueConstraintTrigger(
            fields=["slug"],
            deferrable=Deferrable.DEFERRED,
            name="page_slug_deferred",
        )
        trigger.install(Page)
        return trigger

    def test_deferred_fires_at_commit(self):
        trigger = self._install_deferred()
        try:
            Page.objects.create(slug="existing")
            # Inside atomic, the INSERT succeeds (trigger is deferred).
            # IntegrityError propagates when atomic() tries to commit.
            with pytest.raises(IntegrityError), transaction.atomic():
                Page.objects.create(slug="existing")
        finally:
            trigger.uninstall(Page)

    def test_deferred_duplicate_insert_blocked(self):
        trigger = self._install_deferred()
        try:
            Page.objects.create(slug="dup")
            with pytest.raises(IntegrityError):
                Page.objects.create(slug="dup")
        finally:
            trigger.uninstall(Page)

    def test_deferred_different_values_allowed(self):
        trigger = self._install_deferred()
        try:
            Page.objects.create(slug="one")
            Page.objects.create(slug="two")
        finally:
            trigger.uninstall(Page)


# ---------------------------------------------------------------------------
# FK-traversal fields, non-deferred
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
class TestFKTraversalNonDeferred:
    """FK-traversal: fields=["name", "series__publisher"] (2-hop chain)."""

    def test_same_name_different_publisher_allowed(self):
        pub_a = Publisher.objects.create(name="Publisher A")
        pub_b = Publisher.objects.create(name="Publisher B")
        series_a = Series.objects.create(title="Series A", publisher=pub_a)
        series_b = Series.objects.create(title="Series B", publisher=pub_b)
        Chapter.objects.create(name="Introduction", series=series_a)
        Chapter.objects.create(name="Introduction", series=series_b)  # different publisher, OK

    def test_same_name_same_publisher_blocked(self):
        pub = Publisher.objects.create(name="Publisher")
        series_1 = Series.objects.create(title="Series 1", publisher=pub)
        series_2 = Series.objects.create(title="Series 2", publisher=pub)
        Chapter.objects.create(name="Introduction", series=series_1)
        # Same publisher via different series — should be blocked
        with pytest.raises(IntegrityError):
            Chapter.objects.create(name="Introduction", series=series_2)

    def test_same_name_same_publisher_same_series_blocked(self):
        pub = Publisher.objects.create(name="Publisher")
        series = Series.objects.create(title="Series", publisher=pub)
        Chapter.objects.create(name="Chapter 1", series=series)
        with pytest.raises(IntegrityError):
            Chapter.objects.create(name="Chapter 1", series=series)

    def test_different_name_same_publisher_allowed(self):
        pub = Publisher.objects.create(name="Publisher")
        series = Series.objects.create(title="Series", publisher=pub)
        Chapter.objects.create(name="Chapter 1", series=series)
        Chapter.objects.create(name="Chapter 2", series=series)

    def test_update_to_duplicate_blocked(self):
        pub = Publisher.objects.create(name="Publisher")
        series = Series.objects.create(title="Series", publisher=pub)
        Chapter.objects.create(name="Existing", series=series)
        chapter = Chapter.objects.create(name="Other", series=series)
        chapter.name = "Existing"
        with pytest.raises(IntegrityError):
            chapter.save()

    def test_reassign_series_creates_conflict(self):
        """Moving a chapter to a series under a publisher that already has that name."""
        pub_a = Publisher.objects.create(name="A")
        pub_b = Publisher.objects.create(name="B")
        series_a = Series.objects.create(title="SA", publisher=pub_a)
        series_b = Series.objects.create(title="SB", publisher=pub_b)
        Chapter.objects.create(name="Intro", series=series_a)
        chapter_b = Chapter.objects.create(name="Intro", series=series_b)
        # Move chapter_b to series under publisher A — now conflicts
        series_a2 = Series.objects.create(title="SA2", publisher=pub_a)
        chapter_b.series = series_a2
        with pytest.raises(IntegrityError):
            chapter_b.save()


# ---------------------------------------------------------------------------
# FK-traversal fields, deferred
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
class TestFKTraversalDeferred:
    """FK traversal with deferrable trigger."""

    def _install_deferred(self):
        trigger = UniqueConstraintTrigger(
            fields=["name", "series__publisher"],
            deferrable=Deferrable.DEFERRED,
            name="chapter_name_pub_deferred",
        )
        trigger.install(Chapter)
        return trigger

    def test_deferred_fires_at_commit(self):
        trigger = self._install_deferred()
        try:
            pub = Publisher.objects.create(name="Pub")
            series = Series.objects.create(title="S", publisher=pub)
            Chapter.objects.create(name="Ch", series=series)
            with pytest.raises(IntegrityError), transaction.atomic():
                Chapter.objects.create(name="Ch", series=series)
        finally:
            trigger.uninstall(Chapter)

    def test_deferred_different_publisher_allowed(self):
        trigger = self._install_deferred()
        try:
            pub_a = Publisher.objects.create(name="A")
            pub_b = Publisher.objects.create(name="B")
            s_a = Series.objects.create(title="SA", publisher=pub_a)
            s_b = Series.objects.create(title="SB", publisher=pub_b)
            Chapter.objects.create(name="Intro", series=s_a)
            Chapter.objects.create(name="Intro", series=s_b)
        finally:
            trigger.uninstall(Chapter)


# ---------------------------------------------------------------------------
# Multi-column, non-deferred
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
class TestMultiColumn:
    """Multi-column uniqueness: fields=["slug", "section"]."""

    def _install(self):
        # Disable the model's single-field trigger so it doesn't interfere.
        Page._meta.triggers[0].uninstall(Page)
        trigger = UniqueConstraintTrigger(
            fields=["slug", "section"],
            name="page_slug_section_unique",
        )
        trigger.install(Page)
        return trigger

    def _cleanup(self, trigger):
        trigger.uninstall(Page)
        Page._meta.triggers[0].install(Page)

    def test_same_slug_different_section_allowed(self):
        trigger = self._install()
        try:
            Page.objects.create(slug="hello", section="blog")
            Page.objects.create(slug="hello", section="docs")
        finally:
            self._cleanup(trigger)

    def test_same_slug_same_section_blocked(self):
        trigger = self._install()
        try:
            Page.objects.create(slug="hello", section="blog")
            with pytest.raises(IntegrityError):
                Page.objects.create(slug="hello", section="blog")
        finally:
            self._cleanup(trigger)

    def test_different_slug_same_section_allowed(self):
        trigger = self._install()
        try:
            Page.objects.create(slug="alpha", section="blog")
            Page.objects.create(slug="beta", section="blog")
        finally:
            self._cleanup(trigger)


# ---------------------------------------------------------------------------
# Condition (partial unique)
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
class TestCondition:
    """Partial uniqueness via condition=Q(...)."""

    def _install(self):
        from django.db.models import Q  # noqa: PLC0415

        Page._meta.triggers[0].uninstall(Page)
        trigger = UniqueConstraintTrigger(
            fields=["slug"],
            condition=Q(section="published"),
            name="page_slug_unique_when_published",
        )
        trigger.install(Page)
        return trigger

    def _cleanup(self, trigger):
        trigger.uninstall(Page)
        Page._meta.triggers[0].install(Page)

    def test_duplicate_in_published_blocked(self):
        trigger = self._install()
        try:
            Page.objects.create(slug="hello", section="published")
            with pytest.raises(IntegrityError):
                Page.objects.create(slug="hello", section="published")
        finally:
            self._cleanup(trigger)

    def test_duplicate_in_draft_allowed(self):
        trigger = self._install()
        try:
            Page.objects.create(slug="hello", section="draft")
            Page.objects.create(slug="hello", section="draft")  # condition not met, OK
        finally:
            self._cleanup(trigger)

    def test_duplicate_across_sections_allowed(self):
        trigger = self._install()
        try:
            Page.objects.create(slug="hello", section="published")
            Page.objects.create(slug="hello", section="draft")  # different section, OK
        finally:
            self._cleanup(trigger)


# ---------------------------------------------------------------------------
# NULLS NOT DISTINCT
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
class TestNullsNotDistinct:
    """nulls_distinct=False: two NULLs violate uniqueness."""

    def _install(self):
        Page._meta.triggers[0].uninstall(Page)
        trigger = UniqueConstraintTrigger(
            fields=["slug"],
            nulls_distinct=False,
            name="page_slug_nulls_not_distinct",
        )
        trigger.install(Page)
        return trigger

    def _cleanup(self, trigger):
        trigger.uninstall(Page)
        Page._meta.triggers[0].install(Page)

    def test_two_nulls_blocked(self):
        trigger = self._install()
        try:
            Page.objects.create(slug=None)
            with pytest.raises(IntegrityError):
                Page.objects.create(slug=None)
        finally:
            self._cleanup(trigger)

    def test_null_and_value_allowed(self):
        trigger = self._install()
        try:
            Page.objects.create(slug=None)
            Page.objects.create(slug="hello")
        finally:
            self._cleanup(trigger)

    def test_duplicate_non_null_still_blocked(self):
        trigger = self._install()
        try:
            Page.objects.create(slug="hello")
            with pytest.raises(IntegrityError):
                Page.objects.create(slug="hello")
        finally:
            self._cleanup(trigger)


# ---------------------------------------------------------------------------
# Expressions
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
class TestExpressions:
    """Expression-based uniqueness."""

    def _install(self, trigger):
        Page._meta.triggers[0].uninstall(Page)
        trigger.install(Page)

    def _cleanup(self, trigger):
        trigger.uninstall(Page)
        Page._meta.triggers[0].install(Page)

    def test_lower_blocks_case_insensitive_duplicate(self):
        trigger = UniqueConstraintTrigger(Lower("slug"), name="page_slug_lower")
        self._install(trigger)
        try:
            Page.objects.create(slug="Hello")
            with pytest.raises(IntegrityError):
                Page.objects.create(slug="hello")
        finally:
            self._cleanup(trigger)

    def test_lower_allows_different_values(self):
        trigger = UniqueConstraintTrigger(Lower("slug"), name="page_slug_lower")
        self._install(trigger)
        try:
            Page.objects.create(slug="alpha")
            Page.objects.create(slug="beta")
        finally:
            self._cleanup(trigger)

    def test_lower_update_to_case_insensitive_duplicate(self):
        trigger = UniqueConstraintTrigger(Lower("slug"), name="page_slug_lower")
        self._install(trigger)
        try:
            Page.objects.create(slug="Hello")
            page = Page.objects.create(slug="World")
            page.slug = "HELLO"
            with pytest.raises(IntegrityError):
                page.save()
        finally:
            self._cleanup(trigger)
