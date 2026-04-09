from django.db import models

from django_pgconstraints import CrossTableUnique


class Page(models.Model):
    slug = models.SlugField(unique=True)

    class Meta:
        constraints = [
            CrossTableUnique(
                field="slug",
                across="testapp.Post",
                name="testapp_page_unique_slug_across_post",
            ),
        ]


class Post(models.Model):
    slug = models.SlugField(unique=True)

    class Meta:
        constraints = [
            CrossTableUnique(
                field="slug",
                across="testapp.Page",
                name="testapp_post_unique_slug_across_page",
            ),
        ]
