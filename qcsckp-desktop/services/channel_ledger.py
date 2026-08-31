"""Route only execution reconciliation and cooldowns to the shared ledger.

Business tables and history remain in each profile. SQL routing is confined
to DML table positions, never string literals, schema DDL or arbitrary SQL.
"""
from __future__ import annotations

import re
import sqlite3
import threading
from contextlib import closing
from pathlib import Path

from channel_runtime import layout

TABLES = ("execution_reconciliation", "pmc_retargeting_rate_limit",
          "pmc_retargeting_rate_limit_strategy")
_LOCK = threading.RLock()
_INITIALIZED = set()
_PATTERN = re.compile(r'\b(FROM|JOIN|UPDATE|INTO)\s+(["`]?)(%s)\2(?=\s|\(|$)' % "|".join(TABLES), re.I)


def route_sql(sql):
    if not re.match(r"\s*(SELECT|INSERT|UPDATE|DELETE|REPLACE|WITH)\b", sql, re.I):
        return sql
    parts = re.split(r"('(?:''|[^'])*')", sql)
    for i in range(0, len(parts), 2):
        parts[i] = _PATTERN.sub(lambda m: m[1] + ' channel_guard."' + m[3] + '"', parts[i])
    return "".join(parts)


class GuardCursor(sqlite3.Cursor):
    def execute(self, sql, parameters=()):
        return super().execute(route_sql(sql), parameters)

    def executemany(self, sql, parameters):
        return super().executemany(route_sql(sql), parameters)


class GuardConnection(sqlite3.Connection):
    def cursor(self, factory=GuardCursor):
        return super().cursor(factory)

    def execute(self, sql, parameters=()):
        return self.cursor().execute(sql, parameters)

    def executemany(self, sql, parameters):
        return self.cursor().executemany(sql, parameters)


def managed_database(database):
    return Path(database).resolve() == (layout().data / "qianchuan.db").resolve()


def attach(connection, schemas):
    paths = layout()
    paths.shared.mkdir(parents=True, exist_ok=True)
    guard_path = paths.shared / "execution.sqlite3"
    existed = guard_path.exists()
    cursor = sqlite3.Connection.cursor(connection, sqlite3.Cursor)
    cursor.execute("ATTACH DATABASE ? AS channel_guard", (str(guard_path),))
    with _LOCK:
        if existed and str(guard_path) in _INITIALIZED:
            cursor.close()
            return
        cursor.execute("CREATE TABLE IF NOT EXISTS channel_guard.seeded (table_name TEXT PRIMARY KEY)")
        for table in TABLES:
            schema = schemas[table]
            columns = schema["columns"]
            definition = ",".join('"' + k + '" ' + v for k, v in columns.items())
            cursor.execute(f'CREATE TABLE IF NOT EXISTS channel_guard."{table}" ({definition})')
            actual = {r[1] for r in cursor.execute(f'PRAGMA channel_guard.table_info("{table}")')}
            for name, spec in columns.items():
                if name not in actual:
                    cursor.execute(f'ALTER TABLE channel_guard."{table}" ADD COLUMN "{name}" {spec}')
            for name, cols in schema.get("unique_indexes", []):
                cursor.execute(f'CREATE UNIQUE INDEX IF NOT EXISTS channel_guard."{name}" ON "{table}" ({cols})')
            for name, cols in schema.get("indexes", []):
                cursor.execute(f'CREATE INDEX IF NOT EXISTS channel_guard."{name}" ON "{table}" ({cols})')
            if cursor.execute("SELECT 1 FROM channel_guard.seeded WHERE table_name=?", (table,)).fetchone():
                continue
            # Initial rollout can reuse the existing profile even when the
            # user chooses a fresh business copy: cooldowns must not reset.
            candidates = [paths.data / "qianchuan.db", paths.legacy / "qianchuan.db"]
            for source in candidates:
                if not source.exists():
                    continue
                with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as src:
                    names = [r[1] for r in src.execute(f'PRAGMA table_info("{table}")')]
                    common = [x for x in columns if x in names and x != "id"]
                    if not common:
                        continue
                    fields = ",".join('"' + x + '"' for x in common)
                    for row in src.execute(f'SELECT {fields} FROM "{table}"'):
                        cursor.execute(f'INSERT OR IGNORE INTO channel_guard."{table}" ({fields}) '
                                       f'VALUES ({",".join("?" for _ in common)})', row)
            cursor.execute("INSERT INTO channel_guard.seeded VALUES (?)", (table,))
        connection.commit()
        _INITIALIZED.add(str(guard_path))
    cursor.close()
