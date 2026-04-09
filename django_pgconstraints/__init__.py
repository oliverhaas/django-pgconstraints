"""
Declarative PostgreSQL constraint triggers for Django.
"""

from django_pgconstraints.constraints import (
    AllowedTransitions,
    CheckConstraintTrigger,
    Immutable,
    MaintainedCount,
    UniqueConstraintTrigger,
)

__all__ = [
    "AllowedTransitions",
    "CheckConstraintTrigger",
    "Immutable",
    "MaintainedCount",
    "UniqueConstraintTrigger",
]
