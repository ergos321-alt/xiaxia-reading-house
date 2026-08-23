"""Move legacy book_assets.data values into the private Storage bucket.

Run after migrations/001_prepare_storage_refactor.sql and before
migrations/002_finalize_storage_refactor.sql. New installations do not use this
script.
"""

from __future__ import annotations

import os
import sys

import psycopg
from psycopg.rows import dict_row
from supabase import create_client

from storage import object_path_for_asset


def required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def main() -> int:
    database_url = required("DATABASE_URL")
    supabase_url = required("SUPABASE_URL")
    service_key = required("SUPABASE_SERVICE_ROLE_KEY")
    bucket_name = required("SUPABASE_STORAGE_BUCKET")
    client = create_client(supabase_url, service_key)

    bucket = client.storage.get_bucket(bucket_name)
    is_public = bucket.get("public") if isinstance(bucket, dict) else bucket.public
    if is_public:
        raise RuntimeError("Refusing migration because the Storage bucket is public")

    migrated = 0
    with psycopg.connect(database_url, row_factory=dict_row) as conn:
        while True:
            rows = conn.execute(
                """
                select ba.id, ba.book_id, ba.asset_path, ba.media_type, ba.data,
                       b.cover_asset_path
                from book_assets ba
                join books b on b.id = ba.book_id
                where ba.object_path is null
                order by ba.created_at
                limit 100
                """
            ).fetchall()
            if not rows:
                break
            for row in rows:
                object_path = object_path_for_asset(
                    row["book_id"],
                    row["asset_path"],
                    row["asset_path"] == row["cover_asset_path"],
                )
                client.storage.from_(bucket_name).upload(
                    path=object_path,
                    file=bytes(row["data"]),
                    file_options={
                        "content-type": row["media_type"],
                        "cache-control": "86400",
                        "upsert": "true",
                    },
                )
                conn.execute(
                    """
                    update book_assets
                    set object_path = %s, byte_size = %s
                    where id = %s
                    """,
                    (object_path, len(row["data"]), row["id"]),
                )
                conn.commit()
                migrated += 1

    print(f"Migrated {migrated} legacy asset objects into private Storage.")
    print(
        "Legacy original EPUB/TXT files were never stored in PostgreSQL and "
        "cannot be reconstructed; new uploads retain their source object."
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Migration stopped safely: {exc}", file=sys.stderr)
        raise SystemExit(1)
