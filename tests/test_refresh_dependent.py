"""Tests for refresh_dependent(queryset) public API."""

from decimal import Decimal

import pytest

from django_pgconstraints.triggers import _build_chain_back_where

D = Decimal


@pytest.mark.unit
def test_build_chain_back_where_is_module_level():
    """The chain_back SQL builder must be importable as a module-level helper
    so refresh_dependent and _GeneratedFieldReverse can share it without
    instantiating a reverse trigger."""
    assert callable(_build_chain_back_where)
