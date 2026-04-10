# Simple example — Cross-table unique slugs

Demonstrates `UniqueConstraintTrigger`: Pages and Posts share a URL namespace,
so a slug used by one must not appear in the other.

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
from content.models import Page, Post

Page.objects.create(title="About", slug="about")
Post.objects.create(title="Blog post", slug="about")  # raises IntegrityError
```
