"""Tests for ComputedFieldsReadOnlyAdminMixin."""

import pytest
from django.contrib import admin
from django.contrib.admin.sites import AdminSite
from testapp.models import LineItem, Page, Part, Supplier

from django_pgconstraints import ComputedFieldsReadOnlyAdminMixin


@pytest.mark.unit
def test_mixin_adds_all_generatedfield_targets_to_readonly():
    """A model with two GeneratedFieldTriggers exposes both target fields
    as read-only, with no manually declared readonly_fields."""

    class LineItemAdmin(ComputedFieldsReadOnlyAdminMixin, admin.ModelAdmin):
        pass

    site = AdminSite()
    ma = LineItemAdmin(LineItem, site)
    ro = ma.get_readonly_fields(request=None)
    assert "total" in ro
    assert "slug" in ro


@pytest.mark.unit
def test_mixin_unions_with_manual_readonly_fields():
    """Manually declared readonly_fields are preserved alongside the
    auto-discovered computed fields."""

    class PartAdmin(ComputedFieldsReadOnlyAdminMixin, admin.ModelAdmin):
        readonly_fields = ("name",)

    site = AdminSite()
    ma = PartAdmin(Part, site)
    ro = set(ma.get_readonly_fields(request=None))
    assert "name" in ro  # manual declaration preserved
    assert "markup_amount" in ro  # auto-discovered computed field


@pytest.mark.unit
def test_mixin_noop_on_model_with_no_generatedfield():
    """A model with no GeneratedFieldTrigger in Meta.triggers (e.g., Page
    which has only a UniqueConstraintTrigger) still works — the mixin
    returns whatever the base class would have."""

    class PageAdmin(ComputedFieldsReadOnlyAdminMixin, admin.ModelAdmin):
        readonly_fields = ("slug",)

    site = AdminSite()
    ma = PageAdmin(Page, site)
    ro = ma.get_readonly_fields(request=None)
    assert "slug" in ro
    # No accidental pollution from non-GeneratedFieldTrigger triggers.
    assert len(ro) == 1


@pytest.mark.unit
def test_mixin_returns_tuple_not_list():
    """Django's ModelAdmin convention is tuples for readonly_fields.
    Stick with it so downstream code that does ``a + b`` concatenation
    doesn't break."""

    class SupplierAdmin(ComputedFieldsReadOnlyAdminMixin, admin.ModelAdmin):
        pass

    site = AdminSite()
    ma = SupplierAdmin(Supplier, site)
    ro = ma.get_readonly_fields(request=None)
    assert isinstance(ro, tuple)
