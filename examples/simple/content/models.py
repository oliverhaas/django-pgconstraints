"""Cross-table slug uniqueness between Pages and Posts.

Pages and Posts share a URL namespace — a slug used by one must not be
reused by the other.  Within-table uniqueness is handled by the normal
``unique=True``; the ``UniqueConstraintTrigger`` enforces uniqueness
*across* the two tables.
"""

from django.db import models

from django_pgconstraints import UniqueConstraintTrigger


class Page(models.Model):
    title = models.CharField(max_length=200)
    slug = models.SlugField(unique=True)

    class Meta:
        constraints = [
            UniqueConstraintTrigger(
                field="slug",
                across="content.Post",
                name="content_page_unique_slug_across_post",
            ),
        ]

    def __str__(self):
        return self.title


class Post(models.Model):
    title = models.CharField(max_length=200)
    slug = models.SlugField(unique=True)

    class Meta:
        constraints = [
            UniqueConstraintTrigger(
                field="slug",
                across="content.Page",
                name="content_post_unique_slug_across_page",
            ),
        ]

    def __str__(self):
        return self.title
