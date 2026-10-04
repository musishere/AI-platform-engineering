# Command to create a tenant and print its API key.
#
# The plain key exists only in this command's output: we store the hash, so
# if the tenant loses the key it can't be recovered, only replaced.
#
# Run: uv run --env-file .env python -m gateway.create_tenant <name>

import asyncio
import os
import sys

import asyncpg

from gateway.auth import generate_key, hash_key


async def main(name: str) -> None:
    key = generate_key()
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        tenant_id = await conn.fetchval(
            "INSERT INTO tenants (name, api_key_hash) VALUES ($1, $2) RETURNING id",
            name,
            hash_key(key),
        )
    except asyncpg.UniqueViolationError:
        sys.exit(f"tenant '{name}' already exists")
    finally:
        await conn.close()
    print(f"created tenant '{name}' (id={tenant_id})")
    print(f"API key, shown once. Store it now:\n{key}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: python -m gateway.create_tenant <name>")
    asyncio.run(main(sys.argv[1]))
