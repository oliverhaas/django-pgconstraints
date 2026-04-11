"""Tests for internal helper functions."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pgtrigger.utils
import pytest
from django.core.exceptions import FieldDoesNotExist
from django.db.models import F, Q
from testapp.models import Chapter, OrderLine, Page, PurchaseItem

from django_pgconstraints.sql import (
    _build_comparison,
    _check_q_to_sql,
    _q_to_sql,
    _resolve_f,
    _resolve_field_ref,
    _sql_value,
)

# ---------------------------------------------------------------------------
# _sql_value
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_sql_value_bool_true():
    assert _sql_value(True) == "TRUE"


@pytest.mark.unit
def test_sql_value_bool_false():
    assert _sql_value(False) == "FALSE"


@pytest.mark.unit
def test_sql_value_str():
    assert _sql_value("hello") == "'hello'"


@pytest.mark.unit
def test_sql_value_str_with_quotes():
    assert _sql_value("it's") == "'it''s'"


@pytest.mark.unit
def test_sql_value_int():
    assert _sql_value(42) == "42"


@pytest.mark.unit
def test_sql_value_float():
    assert _sql_value(3.14) == "3.14"


@pytest.mark.unit
def test_sql_value_none():
    assert _sql_value(None) == "NULL"


@pytest.mark.unit
def test_sql_value_decimal():
    assert _sql_value(Decimal("10.50")) == "10.50"


@pytest.mark.unit
def test_sql_value_datetime():
    dt = datetime(2026, 1, 15, 10, 30, 0, tzinfo=UTC)
    result = _sql_value(dt)
    assert "2026-01-15" in result
    assert "::timestamptz" in result


@pytest.mark.unit
def test_sql_value_date():
    assert _sql_value(date(2026, 1, 15)) == "'2026-01-15'::date"


@pytest.mark.unit
def test_sql_value_timedelta():
    td = timedelta(hours=2, minutes=30)
    result = _sql_value(td)
    assert "::interval" in result
    assert "9000" in result  # 2.5 hours = 9000 seconds


@pytest.mark.unit
def test_sql_value_uuid():
    u = UUID("12345678-1234-5678-1234-567812345678")
    result = _sql_value(u)
    assert "12345678-1234-5678-1234-567812345678" in result
    assert "::uuid" in result


@pytest.mark.unit
def test_sql_value_unsupported_type_raises():
    with pytest.raises(TypeError, match="Cannot convert list"):
        _sql_value([1, 2, 3])  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# _q_to_sql (same-table Q compiler used by UniqueConstraintTrigger.condition)
# ---------------------------------------------------------------------------

qn = pgtrigger.utils.quote


@pytest.mark.unit
def test_q_to_sql_nested_q_combines_with_connector():
    q = Q(section="main") & (Q(slug="a") | Q(slug="b"))
    sql = _q_to_sql(q, Page, qn, row_ref="NEW")
    assert "AND" in sql
    assert "OR" in sql
    assert '"section" = ' in sql


@pytest.mark.unit
def test_q_to_sql_exact_none_becomes_is_null():
    sql = _q_to_sql(Q(slug=None), Page, qn, row_ref="NEW")
    assert sql.endswith("IS NULL")


@pytest.mark.unit
def test_q_to_sql_in_lookup():
    sql = _q_to_sql(Q(section__in=["main", "side"]), Page, qn, row_ref="NEW")
    assert "IN ('main', 'side')" in sql


@pytest.mark.unit
def test_q_to_sql_isnull_true():
    sql = _q_to_sql(Q(slug__isnull=True), Page, qn, row_ref="NEW")
    assert sql.endswith("IS NULL")


@pytest.mark.unit
def test_q_to_sql_isnull_false():
    sql = _q_to_sql(Q(slug__isnull=False), Page, qn, row_ref="NEW")
    assert sql.endswith("IS NOT NULL")


@pytest.mark.unit
def test_q_to_sql_negated_q():
    sql = _q_to_sql(~Q(section="main"), Page, qn, row_ref="NEW")
    assert sql.startswith("NOT (")


@pytest.mark.unit
def test_q_to_sql_unsupported_lookup_raises():
    with pytest.raises(ValueError, match="Unsupported lookup type"):
        _q_to_sql(Q(section__contains="x"), Page, qn, row_ref="NEW")


# ---------------------------------------------------------------------------
# _resolve_field_ref (FK-traversal compiler)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_resolve_field_ref_unknown_field_raises():
    with pytest.raises(ValueError, match="No field resolved"):
        _resolve_field_ref("nonexistent", Page, qn)


@pytest.mark.unit
def test_resolve_field_ref_chain_ends_at_relation_returns_fk_col():
    # "series" is the FK itself — the chain ends at a relation, so the
    # resolver should return the FK column reference.
    sql, lookup = _resolve_field_ref("series", Chapter, qn, row_ref="NEW")
    assert sql == 'NEW."series_id"'
    assert lookup == "exact"


@pytest.mark.unit
def test_resolve_f_rejects_lookup_suffix():
    # _resolve_f goes via _resolve_field_ref, which will try to parse
    # "__contains" as another field hop and raise.
    with pytest.raises((ValueError, FieldDoesNotExist)):
        _resolve_f(F("section__contains"), Page, qn)


# ---------------------------------------------------------------------------
# _build_comparison
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_build_comparison_exact_none():
    assert _build_comparison("x", "exact", None, "NULL") == "x IS NULL"


@pytest.mark.unit
def test_build_comparison_in_list():
    assert _build_comparison("x", "in", [1, 2, 3], "") == "x IN (1, 2, 3)"


@pytest.mark.unit
def test_build_comparison_in_subquery():
    # Non-list RHS falls through to the pre-built SQL.
    assert _build_comparison("x", "in", "subq", "(SELECT 1)") == "x IN ((SELECT 1))"


@pytest.mark.unit
def test_build_comparison_isnull_true():
    assert _build_comparison("x", "isnull", True, "TRUE") == "x IS NULL"


@pytest.mark.unit
def test_build_comparison_isnull_false():
    assert _build_comparison("x", "isnull", False, "FALSE") == "x IS NOT NULL"


@pytest.mark.unit
def test_build_comparison_unsupported_raises():
    with pytest.raises(ValueError, match="Unsupported lookup"):
        _build_comparison("x", "contains", "y", "'y'")


# ---------------------------------------------------------------------------
# _check_q_to_sql (FK-aware Q compiler used by CheckConstraintTrigger)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_check_q_to_sql_nested_q():
    q = Q(quantity__gt=0) & Q(quantity__lte=F("product__stock"))
    sql = _check_q_to_sql(q, OrderLine, qn)
    assert " AND " in sql
    assert "SELECT" in sql  # FK traversal produced a subquery


@pytest.mark.unit
def test_check_q_to_sql_multi_hop_fk():
    # PurchaseItem -> part -> supplier -> markup_pct
    q = Q(quantity__lte=F("part__supplier__markup_pct"))
    sql = _check_q_to_sql(q, PurchaseItem, qn)
    # Two FK hops means a nested SELECT.
    assert sql.count("SELECT") >= 2
