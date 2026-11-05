"""Provision the separate local runtime role using the Django database owner.

Run inside the web image after setting INFINITY_DATABASE_URL. No secrets are printed.
"""

import os
from pathlib import Path
from urllib.parse import unquote, urlsplit

import django
from psycopg import sql


def main() -> None:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    django.setup()
    from django.db import connection

    runtime = urlsplit(os.environ["INFINITY_DATABASE_URL"])
    role = unquote(runtime.username or "")
    password = unquote(runtime.password or "")
    if not role or not password or role == connection.settings_dict["USER"]:
        raise SystemExit("Runtime requires a separate login role and password.")
    if unquote(runtime.path.lstrip("/")) != connection.settings_dict["NAME"]:
        raise SystemExit("Runtime must use the same local database as Django.")
    schema = Path(__file__).resolve().parents[1] / "services/infinity-agent/schema.sql"
    with connection.cursor() as cursor:
        cursor.execute(schema.read_text())
        cursor.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", [role])
        if not cursor.fetchone():
            cursor.execute(sql.SQL("CREATE ROLE {} LOGIN").format(sql.Identifier(role)))
        cursor.execute(
            sql.SQL("ALTER ROLE {} PASSWORD {}").format(sql.Identifier(role), sql.Literal(password))
        )
        cursor.execute(
            sql.SQL("GRANT USAGE ON SCHEMA infinity_runtime TO {}").format(sql.Identifier(role))
        )
        cursor.execute(
            sql.SQL(
                "GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA infinity_runtime TO {}"
            ).format(sql.Identifier(role))
        )
        cursor.execute(
            sql.SQL("GRANT DELETE ON infinity_runtime.provider_calls TO {}").format(
                sql.Identifier(role)
            )
        )
        cursor.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role)))
        try:
            cursor.execute(
                "SELECT has_table_privilege(current_user, 'jobs_agentexecution', 'SELECT')"
            )
            if cursor.fetchone()[0]:
                raise SystemExit("Runtime role unexpectedly has application-table access.")
        finally:
            cursor.execute("RESET ROLE")
    print("Infinity schema and restricted login are ready.")


if __name__ == "__main__":
    main()
