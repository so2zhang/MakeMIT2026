#!/usr/bin/env python3
"""Initialize Vultr managed PostgreSQL schema.

Reads DB connection from environment:
  DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD, DB_SSLMODE
"""

from pathlib import Path
import os

import psycopg


def _env(name: str, default: str | None = None, required: bool = False) -> str:
    value = os.getenv(name, default)
    if required and not value:
        raise RuntimeError(f"Missing required env var: {name}")
    return value or ""


def _load_dotenv() -> None:
    env_path = Path(__file__).with_name(".env")
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


DB = {
    "host": _env("DB_HOST", required=True),
    "port": int(_env("DB_PORT", "5432")),
    "dbname": _env("DB_NAME", "defaultdb"),
    "user": _env("DB_USER", required=True),
    "password": _env("DB_PASSWORD", required=True),
    "sslmode": _env("DB_SSLMODE", "require"),
}


def main():
    _load_dotenv()
    schema_sql = Path(__file__).with_name("vultr_schema.sql").read_text(encoding="utf-8")
    with psycopg.connect(**DB) as conn, conn.cursor() as cur:
        cur.execute(schema_sql)
        conn.commit()
    print("Schema applied successfully.")


if __name__ == "__main__":
    main()
