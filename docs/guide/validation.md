# Validation

`UniqueConstraintTrigger` and `CheckConstraintTrigger` implement the
`validate()` method that Django's built-in constraints use, so they
participate in `Model.full_clean()` and `ModelForm` validation the same
way.

## Calling `full_clean()`

Django picks up `Meta.triggers` entries with a `validate()` method and
runs them during `full_clean()`:

```python
line = OrderLine(product=product, quantity=9999)
line.full_clean()  # ValidationError — CheckConstraintTrigger caught it
```

The triggers are discovered through the normal constraint-validation
path.

## `UniqueConstraintTrigger`

- Plain fields and `__`-separated FK chains are validated by an ORM
  query built from the values on the instance.
- `nulls_distinct=None` / `True` short-circuits when any field value is
  NULL (the default PostgreSQL semantics).
- Expression-based triggers (`UniqueConstraintTrigger(Lower("email"), ...)`)
  are **not** validated in Python. The database trigger is the sole
  enforcement path, matching the behaviour of Django's
  `UniqueConstraint` with expressions.

## `CheckConstraintTrigger`

- Same-table conditions are validated by `Q.check()` against the
  instance's field values, the same way Django's `CheckConstraint` does
  it.
- Conditions that reference related columns via `F("rel__field")` skip
  the Python path. The ORM would have to issue a query for every hop,
  and the trigger will catch the violation at `save()` time anyway.

## Custom error codes and messages

Both triggers accept `violation_error_code` and `violation_error_message`
so the `ValidationError` raised by `full_clean()` carries the same code
and message that the rest of the application expects:

```python
CheckConstraintTrigger(
    condition=Q(quantity__gt=0),
    violation_error_code="invalid_quantity",
    violation_error_message="Quantity must be positive.",
    name="orderline_qty_positive",
)
```

The database-level `IntegrityError` is unaffected. It still comes back
with the PostgreSQL SQLSTATE (`23505` for unique, `23514` for check).

## `GeneratedFieldTrigger`

`GeneratedFieldTrigger` does not implement `validate()`. It computes a
value rather than asserting one, so there is nothing for `full_clean()`
to raise. Whatever the trigger sets is what ends up in the column.
