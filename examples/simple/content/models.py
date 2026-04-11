"""Chapter names unique per publisher, traversing two foreign keys.

Django's built-in ``UniqueConstraint`` can only reference columns on the
current table, so it cannot enforce a uniqueness rule that follows a
``Chapter -> Series -> Publisher`` chain.  ``UniqueConstraintTrigger``
accepts ``__``-separated foreign-key paths and compiles them to a PL/pgSQL
trigger.
"""

from django.db import models

from django_pgconstraints import UniqueConstraintTrigger


class Publisher(models.Model):
    name = models.CharField(max_length=100)


class Series(models.Model):
    title = models.CharField(max_length=200)
    publisher = models.ForeignKey(Publisher, on_delete=models.CASCADE)


class Chapter(models.Model):
    name = models.CharField(max_length=200)
    series = models.ForeignKey(Series, on_delete=models.CASCADE)

    class Meta:
        triggers = [
            UniqueConstraintTrigger(
                fields=["name", "series__publisher"],
                name="chapter_unique_name_per_publisher",
            ),
        ]
