# Shared database pieces used by more than one module (main, metering).

import asyncpg

# What a broken or unreachable Postgres looks like to asyncpg:
#   OSError        - not running at all (connection refused)
#   PostgresError  - running but refusing (e.g. still starting up)
#   InterfaceError - connection dropped mid-query
# Listed explicitly (not `except Exception`) so a real bug in our code still
# surfaces as a 500 instead of hiding behind "database unavailable".
DB_ERRORS = (OSError, asyncpg.PostgresError, asyncpg.InterfaceError)
