"""CLI tool for ingesting productions without running the web server."""

import asyncio
import sys

from app.database import async_session
from app.services.ingest import ingest_production


async def main():
    if len(sys.argv) < 3:
        print("Usage: python ingest_cli.py <production_name> <production_root> [description]")
        sys.exit(1)

    name = sys.argv[1]
    root = sys.argv[2]
    desc = sys.argv[3] if len(sys.argv) > 3 else ""

    async with async_session() as db:
        result = await ingest_production(db, name, root, desc)

    print(f"Ingested {result['documents_ingested']} documents into production '{result['production_name']}'")
    if result["errors"]:
        print(f"\n{result['error_count']} warnings:")
        for err in result["errors"][:20]:
            print(f"  - {err}")
        if result["error_count"] > 20:
            print(f"  ... and {result['error_count'] - 20} more")


if __name__ == "__main__":
    asyncio.run(main())
