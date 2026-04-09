import os

SECRET_KEY = "test-secret-key"

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "testapp",
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("PGDATABASE", "django_pgconstraints_test"),
        "USER": os.environ.get("PGUSER", "postgres"),
        "PASSWORD": os.environ.get("PGPASSWORD", "postgres"),
        "HOST": os.environ.get("PGHOST", "/tmp/pgrun"),
        "PORT": os.environ.get("PGPORT", "5432"),
    },
}

MIGRATION_MODULES = {
    "testapp": None,
}

USE_TZ = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
