"""Tests for internal helper functions."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from django_pgconstraints.sql import _sql_value

# ---------------------------------------------------------------------------
# _sql_value
# ---------------------------------------------------------------------------


class TestSqlValue:
    def test_bool_true(self):
        assert _sql_value(True) == "TRUE"

    def test_bool_false(self):
        assert _sql_value(False) == "FALSE"

    def test_str(self):
        assert _sql_value("hello") == "'hello'"

    def test_str_with_quotes(self):
        assert _sql_value("it's") == "'it''s'"

    def test_int(self):
        assert _sql_value(42) == "42"

    def test_float(self):
        assert _sql_value(3.14) == "3.14"

    def test_none(self):
        assert _sql_value(None) == "NULL"

    def test_decimal(self):
        assert _sql_value(Decimal("10.50")) == "10.50"

    def test_datetime(self):
        dt = datetime(2026, 1, 15, 10, 30, 0, tzinfo=UTC)
        result = _sql_value(dt)
        assert "2026-01-15" in result
        assert "::timestamptz" in result

    def test_date(self):
        d = date(2026, 1, 15)
        assert _sql_value(d) == "'2026-01-15'::date"

    def test_timedelta(self):
        td = timedelta(hours=2, minutes=30)
        result = _sql_value(td)
        assert "::interval" in result
        assert "9000" in result  # 2.5 hours = 9000 seconds

    def test_uuid(self):
        u = UUID("12345678-1234-5678-1234-567812345678")
        result = _sql_value(u)
        assert "12345678-1234-5678-1234-567812345678" in result
        assert "::uuid" in result

    def test_unsupported_type(self):
        with pytest.raises(TypeError, match="Cannot convert list"):
            _sql_value([1, 2, 3])  # type: ignore[arg-type]
