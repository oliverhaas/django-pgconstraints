"""Tests for cycle detection in GeneratedFieldTrigger dependencies."""

import pytest
from django.db.models import F

from django_pgconstraints.cycles import CycleError, _detect, check_for_cycles

# ---------------------------------------------------------------------------
# _detect — unit tests on synthetic adjacency maps
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_detect_empty_is_ok():
    _detect({}, set())


@pytest.mark.unit
def test_detect_linear_chain_is_ok():
    a, b, c = ("app.A", "x"), ("app.B", "y"), ("app.C", "z")
    _detect({a: {b}, b: {c}}, {a, b, c})


@pytest.mark.unit
def test_detect_diamond_is_ok():
    """A → B, A → C, B → D, C → D is acyclic."""
    a, b, c, d = ("A", "x"), ("B", "y"), ("C", "z"), ("D", "w")
    _detect({a: {b, c}, b: {d}, c: {d}}, {a, b, c, d})


@pytest.mark.unit
def test_detect_direct_two_node_cycle_raises():
    a, b = ("A", "x"), ("B", "y")
    with pytest.raises(CycleError) as exc_info:
        _detect({a: {b}, b: {a}}, {a, b})
    assert "A.x" in str(exc_info.value)
    assert "B.y" in str(exc_info.value)
    assert "→" in str(exc_info.value)


@pytest.mark.unit
def test_detect_self_loop_raises():
    a = ("A", "x")
    with pytest.raises(CycleError) as exc_info:
        _detect({a: {a}}, {a})
    # Path starts and ends with the same node.
    assert exc_info.value.path[0] == exc_info.value.path[-1]


@pytest.mark.unit
def test_detect_three_node_cycle_raises():
    a, b, c = ("A", "x"), ("B", "y"), ("C", "z")
    with pytest.raises(CycleError):
        _detect({a: {b}, b: {c}, c: {a}}, {a, b, c})


@pytest.mark.unit
def test_detect_isolated_cyclic_component_still_found():
    """An acyclic component next to a cyclic one must not mask the cycle."""
    x, y = ("OK", "x"), ("OK", "y")
    p, q = ("BAD", "p"), ("BAD", "q")
    with pytest.raises(CycleError):
        _detect({x: {y}, p: {q}, q: {p}}, {x, y, p, q})


# ---------------------------------------------------------------------------
# check_for_cycles — integration with real testapp models
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_check_for_cycles_real_testapp_is_acyclic():
    """The three models in testapp/models.py must not trip the cycle check."""
    from django.apps import apps  # noqa: PLC0415

    from django_pgconstraints.triggers import GeneratedFieldTrigger  # noqa: PLC0415

    specs = [
        (m, t)
        for m in apps.get_models()
        for t in getattr(m._meta, "triggers", [])
        if isinstance(t, GeneratedFieldTrigger)
    ]
    check_for_cycles(specs)  # must not raise


@pytest.mark.django_db
def test_check_for_cycles_fake_cyclic_specs_raises():
    """Two synthetic triggers on LineItem that reference each other's field.

    The triggers are never installed — we feed them directly to
    check_for_cycles to prove the cycle walk works against real model meta.
    """
    from testapp.models import LineItem  # noqa: PLC0415

    from django_pgconstraints import GeneratedFieldTrigger  # noqa: PLC0415

    spec1 = (
        LineItem,
        GeneratedFieldTrigger(
            field="total",
            expression=F("slug"),
            name="fake_total_depends_on_slug",
        ),
    )
    spec2 = (
        LineItem,
        GeneratedFieldTrigger(
            field="slug",
            expression=F("total"),
            name="fake_slug_depends_on_total",
        ),
    )

    with pytest.raises(CycleError) as exc_info:
        check_for_cycles([spec1, spec2])

    message = str(exc_info.value)
    assert "testapp.LineItem.total" in message
    assert "testapp.LineItem.slug" in message
