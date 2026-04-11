# Full example — Inventory and purchase orders

A small e-commerce-ish domain that exercises every trigger the package
provides, each used to enforce or compute something that Django's
built-in constraints can't do because the rule crosses a foreign key.

| Trigger                    | Rule                                                                    |
|----------------------------|-------------------------------------------------------------------------|
| `UniqueConstraintTrigger`  | `Part.sku` is unique *within* each supplier (FK traversal)              |
| `CheckConstraintTrigger`   | `PurchaseLine.quantity <= part.stock` and `quantity > 0`                |
| `GeneratedFieldTrigger`    | `Part.markup_amount = base_price * supplier.markup_pct / 100`           |
| `GeneratedFieldTrigger`    | `PurchaseLine.line_total = quantity * part.base_price`                  |

The two generated fields also install **reverse triggers** automatically:
if a `Supplier.markup_pct` changes, every related `Part.markup_amount`
recomputes; if a `Part.base_price` changes, every related
`PurchaseLine.line_total` recomputes.

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
from shop.models import Supplier, Part, PurchaseOrder, PurchaseLine

acme = Supplier.objects.create(name="Acme", markup_pct=20)

# Generated field — markup_amount is computed by the trigger.
widget = Part.objects.create(sku="W-001", name="Widget", supplier=acme, base_price=50, stock=100)
widget.refresh_from_db()
widget.markup_amount  # Decimal('10.00') — 50 * 20 / 100

# Unique within supplier — a different supplier can reuse the SKU.
globex = Supplier.objects.create(name="Globex", markup_pct=15)
Part.objects.create(sku="W-001", name="Widget (Globex)", supplier=globex, base_price=40, stock=50)  # OK
Part.objects.create(sku="W-001", name="Duplicate", supplier=acme, base_price=60, stock=10)          # IntegrityError

# Check constraint — can't order more than stock.
order = PurchaseOrder.objects.create(reference="PO-0001")
PurchaseLine.objects.create(order=order, part=widget, quantity=10_000)  # IntegrityError

line = PurchaseLine.objects.create(order=order, part=widget, quantity=3)
line.refresh_from_db()
line.line_total  # Decimal('150.00') — 3 * 50

# Reverse trigger — changing the supplier's markup updates every related Part.
acme.markup_pct = 30
acme.save()
widget.refresh_from_db()
widget.markup_amount  # Decimal('15.00') — recomputed automatically
```
