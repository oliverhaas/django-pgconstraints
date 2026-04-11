"""Tests for Django system checks."""

import pytest
from django.core.checks import Error
from testapp.models import Page

from django_pgconstraints.apps import check_triggers_not_in_constraints
from django_pgconstraints.triggers import UniqueConstraintTrigger


@pytest.mark.django_db
class TestTriggersNotInConstraints:
    def test_no_errors_when_triggers_in_correct_location(self):
        errors = check_triggers_not_in_constraints()
        assert errors == []

    def test_error_when_trigger_in_constraints(self):
        fake = UniqueConstraintTrigger(
            field="slug",
            across="testapp.Post",
            name="misplaced_trigger",
        )

        Page._meta.constraints.append(fake)
        try:
            errors = check_triggers_not_in_constraints()
            assert len(errors) == 1
            assert isinstance(errors[0], Error)
            assert "Meta.triggers" in errors[0].msg
            assert errors[0].id == "pgconstraints.E001"
        finally:
            Page._meta.constraints.remove(fake)
