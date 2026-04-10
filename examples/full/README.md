# Full example — E-commerce with all constraint types

Demonstrates all five constraint types in a realistic e-commerce domain.

| Constraint | What it does |
|---|---|
| `UniqueConstraintTrigger` | SKUs are unique across `Product` and `ArchivedProduct` |
| `AllowedTransitions` | Orders follow draft → pending → paid → shipped → delivered |
| `Immutable` | Invoice amount cannot change once `status="paid"` |
| `MaintainedCount` | `Category.product_count` stays in sync automatically |
| `CheckConstraintTrigger` | `OrderLine.quantity` must not exceed `Product.stock` |

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
from shop.models import *

# Cross-table uniqueness
cat = Category.objects.create(name="Electronics")
p = Product.objects.create(name="Widget", sku="W-001", stock=50, category=cat)
ArchivedProduct.objects.create(name="Old Widget", sku="W-001")  # IntegrityError!

# Maintained count
cat.refresh_from_db()
cat.product_count  # 1 — automatically maintained

# State machine
order = Order.objects.create()
order.status = "shipped"
order.save()  # IntegrityError — must go draft → pending first

# Immutable fields
inv = Invoice.objects.create(order=order, amount=99.99, status="paid")
inv.amount = 50.00
inv.save()  # IntegrityError — amount is locked

# Stock check
line = OrderLine.objects.create(order=order, product=p, quantity=999)  # IntegrityError
```
