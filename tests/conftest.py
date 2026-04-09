"""Pytest configuration — spins up a PostgreSQL container via testcontainers."""

import os

import django
import pytest
from django.conf import settings
from testcontainers.postgres import PostgresContainer


@pytest.fixture(scope="session", autouse=True)
def _postgres_container():
    """Start a disposable PostgreSQL container for the entire test session."""
    with PostgresContainer("postgres:17", driver=None) as pg:
        os.environ["PGDATABASE"] = pg.dbname
        os.environ["PGUSER"] = pg.username
        os.environ["PGPASSWORD"] = pg.password
        os.environ["PGHOST"] = pg.get_container_host_ip()
        os.environ["PGPORT"] = str(pg.get_exposed_port(pg.port))

        # Re-configure Django now that the env vars point at the container.
        settings.DATABASES["default"].update(
            {
                "NAME": pg.dbname,
                "USER": pg.username,
                "PASSWORD": pg.password,
                "HOST": pg.get_container_host_ip(),
                "PORT": str(pg.get_exposed_port(pg.port)),
            },
        )

        # Ensure Django apps are ready (idempotent if already initialised).
        django.setup()

        yield pg
