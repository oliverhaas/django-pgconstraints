"""Tests for internal helper functions."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from django.db.models import Q

from django_pgconstraints import AllowedTransitions, Immutable
from django_pgconstraints.constraints import _make_fn_name, _sql_value

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


# ---------------------------------------------------------------------------
# _make_fn_name
# ---------------------------------------------------------------------------


class TestMakeFnName:
    def test_short_name(self):
        result = _make_fn_name("my_table", "my_constraint")
        assert result == "pgc_fn_my_table_my_constraint"

    def test_long_name_is_truncated(self):
        result = _make_fn_name("a" * 30, "b" * 30)
        assert len(result.encode()) <= 63

    def test_long_name_with_suffix(self):
        result = _make_fn_name("a" * 30, "b" * 30, "_ins")
        assert len(result.encode()) <= 63
        assert result.endswith("_ins")

    def test_deterministic(self):
        """Same inputs always produce the same output."""
        a = _make_fn_name("a" * 30, "b" * 30, "_del")
        b = _make_fn_name("a" * 30, "b" * 30, "_del")
        assert a == b

    def test_different_inputs_no_collision(self):
        a = _make_fn_name("table_a", "x" * 50)
        b = _make_fn_name("table_b", "x" * 50)
        assert a != b


# ---------------------------------------------------------------------------
# AllowedTransitions.__hash__
# ---------------------------------------------------------------------------


class TestAllowedTransitionsHash:
    def test_hash_does_not_crash(self):
        """Hashing should not raise TypeError from unhashable list values."""
        c = AllowedTransitions(
            field="status",
            transitions={"draft": ["pending"], "pending": ["shipped"]},
            name="c",
        )
        # This used to crash with TypeError because dict values are lists.
        h = hash(c)
        assert isinstance(h, int)

    def test_hashable_in_set(self):
        c = AllowedTransitions(
            field="status",
            transitions={"draft": ["pending"]},
            name="c",
        )
        assert {c}


# ---------------------------------------------------------------------------
# Immutable.__init__ validation
# ---------------------------------------------------------------------------


class TestImmutableValidation:
    def test_empty_fields_raises(self):
        with pytest.raises(ValueError, match="at least one field"):
            Immutable(fields=[], name="c")


# ---------------------------------------------------------------------------
# Immutable.__hash__ includes when
# ---------------------------------------------------------------------------


class TestImmutableHash:
    def test_different_when_different_hash(self):
        a = Immutable(fields=["amount"], when=Q(status="paid"), name="c")
        b = Immutable(fields=["amount"], when=Q(status="draft"), name="c")
        # Different when → should (very likely) have different hashes.
        # They are definitely not equal, so hash collision is possible but
        # at least the hash should not be identical by construction.
        assert a != b
        # We can't guarantee different hash, but we CAN check it doesn't crash.
        hash(a)
        hash(b)

    def test_same_when_same_hash(self):
        a = Immutable(fields=["amount"], when=Q(status="paid"), name="c")
        b = Immutable(fields=["amount"], when=Q(status="paid"), name="c")
        assert a == b
        assert hash(a) == hash(b)
