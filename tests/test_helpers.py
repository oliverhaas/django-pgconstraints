"""Tests for internal helper functions."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from django_pgconstraints.sql import _sql_value

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
