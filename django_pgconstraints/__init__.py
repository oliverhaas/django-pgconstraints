"""
Declarative PostgreSQL constraint triggers for Django.
"""

from django_pgconstraints.constraints import AllowedTransitions, CrossTableUnique, Immutable, MaintainedCount

__all__ = ["AllowedTransitions", "CrossTableUnique", "Immutable", "MaintainedCount"]
