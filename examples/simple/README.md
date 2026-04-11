# Simple example — Cross-table unique chapter names

A `Publisher` owns many `Series`, a `Series` owns many `Chapter`s, and we
want chapter names to be unique *per publisher* — not per series.  Django's
built-in `UniqueConstraint` can't express this because the uniqueness key
lives two tables away from `Chapter`.

`UniqueConstraintTrigger` accepts foreign-key chains directly:

```python
UniqueConstraintTrigger(
    fields=["name", "series__publisher"],
    name="chapter_unique_name_per_publisher",
)
```

## Setup

```bash
uv sync
uv run python -m django migrate --settings=config.settings
```

## Try it

```bash
uv run python -m django shell --settings=config.settings
```

```python
from content.models import Publisher, Series, Chapter

penguin = Publisher.objects.create(name="Penguin")
orbit = Publisher.objects.create(name="Orbit")

dune = Series.objects.create(title="Dune", publisher=penguin)
foundation = Series.objects.create(title="Foundation", publisher=penguin)
expanse = Series.objects.create(title="The Expanse", publisher=orbit)

Chapter.objects.create(name="Beginnings", series=dune)
Chapter.objects.create(name="Beginnings", series=expanse)      # OK — different publisher
Chapter.objects.create(name="Beginnings", series=foundation)   # IntegrityError
```
