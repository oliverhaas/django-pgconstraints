SECRET_KEY = "example-only-not-for-production"

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "content",
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": "simple_example",
    },
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
