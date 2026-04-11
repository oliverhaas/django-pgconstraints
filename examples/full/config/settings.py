SECRET_KEY = "example-only-not-for-production"

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "pgtrigger",
    "django_pgconstraints",
    "shop",
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": "full_example",
    },
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
