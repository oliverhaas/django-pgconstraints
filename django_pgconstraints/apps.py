"""Django app configuration for django-pgconstraints."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.apps import AppConfig
from django.core.checks import Error, Tags, register

if TYPE_CHECKING:
    from django.core.checks import CheckMessage


class PgConstraintsConfig(AppConfig):
    name = "django_pgconstraints"

    def ready(self) -> None:
        _register_reverse_triggers()


def _register_reverse_triggers() -> None:
    """Scan all models for GeneratedFieldTrigger and register reverse triggers."""
    from django.apps import apps  # noqa: PLC0415

    from django_pgconstraints.triggers import GeneratedFieldTrigger  # noqa: PLC0415

    for model in apps.get_models():
        for trigger in getattr(model._meta, "triggers", []):  # noqa: SLF001
            if isinstance(trigger, GeneratedFieldTrigger):
                for related_model, reverse_trigger in trigger.get_reverse_triggers(model):
                    reverse_trigger.register(related_model)  # type: ignore[arg-type]


@register(Tags.models)
def check_triggers_not_in_constraints(**kwargs: Any) -> list[CheckMessage]:  # noqa: ANN401, ARG001
    """Raise an error if a pgconstraints trigger is placed in Meta.constraints."""
    from django.apps import apps  # noqa: PLC0415

    from django_pgconstraints.triggers import (  # noqa: PLC0415
        CheckConstraintTrigger,
        GeneratedFieldTrigger,
        UniqueConstraintTrigger,
    )

    trigger_types = (
        UniqueConstraintTrigger,
        CheckConstraintTrigger,
        GeneratedFieldTrigger,
    )

    return [
        Error(
            f"{constraint.__class__.__name__} belongs in Meta.triggers, not Meta.constraints.",
            hint="See django-pgtrigger docs for Meta.triggers usage.",
            obj=model,
            id="pgconstraints.E001",
        )
        for model in apps.get_models()
        for constraint in model._meta.constraints  # noqa: SLF001
        if isinstance(constraint, trigger_types)
    ]
