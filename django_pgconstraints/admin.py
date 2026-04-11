"""Django admin mixins for django-pgconstraints."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django_pgconstraints.triggers import GeneratedFieldTrigger

if TYPE_CHECKING:
    from django.http import HttpRequest


class ComputedFieldsReadOnlyAdminMixin:
    """ModelAdmin mixin: marks GeneratedFieldTrigger target fields read-only.

    Any field that appears as the ``field=`` argument of a
    ``GeneratedFieldTrigger`` in ``Meta.triggers`` is automatically added
    to the admin's read-only fields, on top of whatever the subclass
    declared manually. Prevents users from accidentally typing into a
    computed field in admin (which would be silently overwritten by the
    BEFORE UPDATE trigger on save).

    Usage::

        @admin.register(Part)
        class PartAdmin(ComputedFieldsReadOnlyAdminMixin, admin.ModelAdmin):
            list_display = ("name", "base_price", "markup_amount")
    """

    def get_readonly_fields(
        self,
        request: HttpRequest | None,
        obj: Any = None,  # noqa: ANN401
    ) -> tuple[str, ...]:
        base = tuple(super().get_readonly_fields(request, obj))  # type: ignore[misc]
        computed = tuple(
            trigger.field
            for trigger in getattr(self.model._meta, "triggers", [])  # type: ignore[attr-defined]  # noqa: SLF001
            if isinstance(trigger, GeneratedFieldTrigger)
        )
        # Preserve declaration order of manual fields, then append
        # computed fields not already present.
        seen = set(base)
        extras = tuple(f for f in computed if f not in seen)
        return base + extras
