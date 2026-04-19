"""Auto-refresh trigger-backed GeneratedFieldTrigger targets via RETURNING.

For each ``GeneratedFieldTrigger`` with ``auto_refresh=True`` (the default),
the package piggybacks the INSERT / UPDATE / bulk_create statements Django
already issues so the trigger-computed value is returned and written back
onto the Python instance without a separate query.

Implementation notes:

- **INSERT via save() and bulk_create()** — Django honors ``Field.db_returning``
  via ``meta.db_returning_fields``.  The base ``Field.db_returning`` is a
  property with no setter, so we can't assign on the instance; instead we
  swap the field instance's class for a thin subclass that declares
  ``db_returning = True`` as a class attribute.  MRO lookup finds the
  subclass attribute before falling through to the property.

- **UPDATE via save()** — Django's ``_save_table`` builds a separate
  ``returning_fields`` list that only includes ``GeneratedField`` (the
  built-in kind with ``f.generated`` True).  It does not consult
  ``db_returning``.  We override ``_do_update`` on the model class to
  extend that list with our tracked fields in place; ``_save_table``
  then uses the same mutated list for ``_assign_returned_values``.

- **Not covered:** ``QuerySet.update()`` and ``bulk_update()`` have no
  in-memory instance to refresh (or, for ``bulk_update``, use a CASE WHEN
  query that does not support RETURNING).  Callers of those APIs must
  refresh instances themselves.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from django.db.models import Field, Model


# One dynamic subclass per original field class, shared across all fields of
# that class that we patch.  Keeps ``isinstance`` checks stable and avoids
# generating a new class per field instance.
_PATCHED_FIELD_CLASSES: dict[type, type] = {}


def register_auto_refresh() -> None:
    """Wire RETURNING-based refresh into every model with a ``GeneratedFieldTrigger``.

    Called once from ``AppConfig.ready()``.  For each trigger with
    ``auto_refresh=True``, marks the target field for RETURNING on
    INSERT / bulk_create and installs an ``_do_update`` override that
    extends the UPDATE path's ``returning_fields`` list.
    """
    from django.apps import apps  # noqa: PLC0415

    from django_pgconstraints.triggers import GeneratedFieldTrigger  # noqa: PLC0415

    model_fields: dict[type[Model], list[str]] = {}
    for model in apps.get_models():
        for trigger in getattr(model._meta, "triggers", []):  # noqa: SLF001
            if not isinstance(trigger, GeneratedFieldTrigger):
                continue
            if not trigger.auto_refresh:
                continue
            model_fields.setdefault(model, []).append(trigger.field)

    for model, field_names in model_fields.items():
        fields: list[Field] = [
            model._meta.get_field(name)  # type: ignore[misc]  # noqa: SLF001
            for name in field_names
        ]
        for field in fields:
            _patch_field_db_returning(field)
        # Bust the cached property so the newly-flagged fields are picked up.
        model._meta.__dict__.pop("db_returning_fields", None)  # noqa: SLF001
        _install_do_update_override(model, fields)


def _patch_field_db_returning(field: Field) -> None:
    """Make ``field.db_returning`` return True via a dynamic subclass swap.

    The base ``Field.db_returning`` is a property (it returns
    ``self.has_db_default()``) — assignment via ``field.db_returning = True``
    raises ``AttributeError`` because the property has no setter.  We work
    around this by creating a thin subclass whose class body declares
    ``db_returning = True``; Python attribute lookup walks the MRO and
    finds the subclass's class attribute before reaching the property on
    ``Field``.
    """
    cls = type(field)
    if cls.__dict__.get("db_returning") is True:
        return
    if cls in _PATCHED_FIELD_CLASSES:
        field.__class__ = _PATCHED_FIELD_CLASSES[cls]
        return
    patched = type(
        f"_AutoReturning{cls.__name__}",
        (cls,),
        {"db_returning": True},
    )
    _PATCHED_FIELD_CLASSES[cls] = patched
    field.__class__ = patched


def _install_do_update_override(model: type[Model], fields: list[Field]) -> None:
    """Override ``_do_update`` on *model* to request RETURNING for our fields.

    ``Model._save_table`` passes a locally-built ``returning_fields`` list
    through to ``_do_update`` and then uses the same list for
    ``_assign_returned_values``.  Mutating the list in place before
    dispatching to the original ``_do_update`` causes the UPDATE SQL to
    include our columns in ``RETURNING`` and the returned values to be
    written onto ``self`` at matching positions.
    """
    if getattr(model, "_pgc_auto_refresh_patched", False):
        tracked: list[Field] = model._pgc_auto_refresh_fields  # type: ignore[attr-defined]  # noqa: SLF001
        for field in fields:
            if field not in tracked:
                tracked.append(field)
        return

    model._pgc_auto_refresh_fields = list(fields)  # type: ignore[attr-defined]  # noqa: SLF001
    original = model._do_update  # noqa: SLF001

    def _do_update(  # noqa: PLR0913
        self: Model,
        base_qs: Any,  # noqa: ANN401
        using: str,
        pk_val: Any,  # noqa: ANN401
        values: Any,  # noqa: ANN401
        update_fields: Any,  # noqa: ANN401
        forced_update: bool,  # noqa: FBT001
        returning_fields: list[Field],
    ) -> Any:  # noqa: ANN401
        for field in type(self)._pgc_auto_refresh_fields:  # type: ignore[attr-defined]  # noqa: SLF001
            if field not in returning_fields:
                returning_fields.append(field)
        return original(
            self,
            base_qs,
            using,
            pk_val,
            values,
            update_fields,
            forced_update,
            returning_fields,
        )

    model._do_update = _do_update  # type: ignore[assignment,method-assign]  # noqa: SLF001
    model._pgc_auto_refresh_patched = True  # type: ignore[attr-defined]  # noqa: SLF001
