from .sqlite_reader import SQLiteReader

try:
    from .pg_reader import PostgresReader
except ImportError:
    PostgresReader = None  # psycopg2 not installed

__all__ = ["SQLiteReader", "PostgresReader"]
