"""Management command that forces recomputation of GeneratedFieldTrigger fields.

Runs ``UPDATE <table> SET <col> = <col>`` on one or more target tables, which
is a no-op at the data level but fires the ``BEFORE UPDATE`` trigger and
recomputes the computed value.  Useful for:

- Backfilling new triggers on existing rows after adding them via migration.
- Resyncing after a bulk load performed under ``pgtrigger.ignore()`` or
  ``ALTER TABLE ... DISABLE TRIGGER``.
- Recomputing existing rows after an expression change.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pgtrigger.utils
from django.apps import apps
from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from django_pgconstraints.triggers import GeneratedFieldTrigger

if TYPE_CHECKING:
    from argparse import ArgumentParser

    from django.db.models import Field, Model


class Command(BaseCommand):
    help = (
        "Refresh GeneratedFieldTrigger-managed fields by touching rows. "
        "Takes an app.Model or app.Model.field target, or --all for every "
        "managed field in the project."
    )

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument(
            "target",
            nargs="?",
            help="app_label.ModelName or app_label.ModelName.field_name",
        )
        parser.add_argument(
            "--all",
            action="store_true",
            dest="all_flag",
            help="Refresh every GeneratedFieldTrigger-managed field.",
        )

    def handle(self, *args: Any, **options: Any) -> None:  # noqa: ANN401, ARG002
        target: str | None = options.get("target")
        all_flag: bool = options.get("all_flag", False)

        if not target and not all_flag:
            msg = "Provide a target (app.Model or app.Model.field) or use --all"
            raise CommandError(msg)

        specs = self._collect_specs(target, all_flag=all_flag)

        qn = pgtrigger.utils.quote
        for model, trigger in specs:
            table = qn(model._meta.db_table)  # noqa: SLF001
            field: Field[Any, Any] = model._meta.get_field(trigger.field)  # type: ignore[assignment]  # noqa: SLF001
            col = qn(field.column)
            sql = f"UPDATE {table} SET {col} = {col}"
            self.stdout.write(
                f"Refreshing {model._meta.label}.{trigger.field} ...",  # noqa: SLF001
            )
            with connection.cursor() as cur:
                cur.execute(sql)
                self.stdout.write(self.style.SUCCESS(f"  {cur.rowcount} rows"))

    def _collect_specs(
        self,
        target: str | None,
        *,
        all_flag: bool,
    ) -> list[tuple[type[Model], GeneratedFieldTrigger]]:
        all_specs: list[tuple[type[Model], GeneratedFieldTrigger]] = [
            (model, trigger)
            for model in apps.get_models()
            for trigger in getattr(model._meta, "triggers", [])  # noqa: SLF001
            if isinstance(trigger, GeneratedFieldTrigger)
        ]

        if all_flag:
            return all_specs

        if target is None:
            msg = "Provide a target (app.Model or app.Model.field) or use --all"
            raise CommandError(msg)
        parts = target.split(".")
        if len(parts) not in (2, 3):
            msg = f"Invalid target {target!r} — expected 'app.Model' or 'app.Model.field'"
            raise CommandError(msg)

        app_label = parts[0]
        model_name = parts[1]
        field_name: str | None = parts[2] if len(parts) == 3 else None  # noqa: PLR2004

        try:
            model = apps.get_model(app_label, model_name)
        except LookupError as exc:
            msg = f"Model not found: {app_label}.{model_name}"
            raise CommandError(msg) from exc

        matched = [(m, t) for m, t in all_specs if m is model and (field_name is None or t.field == field_name)]

        if not matched:
            if field_name is not None:
                msg = f"{app_label}.{model_name} has no GeneratedFieldTrigger on field {field_name!r}"
            else:
                msg = f"{app_label}.{model_name} has no GeneratedFieldTrigger fields"
            raise CommandError(msg)

        return matched
