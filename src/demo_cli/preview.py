"""Preview a destructive SQL statement before it runs: how many rows it would
affect, and a small sample. Read-only. sqlite uses the stdlib driver; postgres
is best-effort via the `psql` client so there is no Python dependency.
"""
from __future__ import annotations

import re
import shutil
import sqlite3
import subprocess
from typing import List, Optional, Tuple

_TRUNCATE = re.compile(r"\bTRUNCATE\b", re.I)


def _parse_table_where(sql: str, verb: str) -> Tuple[Optional[str], Optional[str]]:
    sql = sql.strip().rstrip(";")
    if verb == "DELETE":
        m = re.search(r"^\s*DELETE\s+FROM\s+(\w+)\s*(?:WHERE\s+(.+))?$", sql, re.I | re.S)
    elif verb == "UPDATE":
        m = re.search(r"^\s*UPDATE\s+(\w+)\s+SET\s+.+?(?:\s+WHERE\s+(.+))?$", sql, re.I | re.S)
    else:
        return None, None
    if not m:
        return None, None
    return m.group(1), (m.group(2).strip() if m.group(2) else None)


def _preview_queries(sql: str) -> Tuple[Optional[str], Optional[str]]:
    sql = sql.strip().rstrip(";")
    parts = sql.split()
    verb = parts[0].upper() if parts else ""
    if _TRUNCATE.search(sql):
        m = re.search(r"TRUNCATE\s+(?:TABLE\s+)?(\w+)", sql, re.I)
        if not m:
            return None, None
        t = m.group(1)
        return f"SELECT COUNT(*) FROM {t}", f"SELECT * FROM {t} LIMIT 5"
    if verb in {"DELETE", "UPDATE"}:
        table, where = _parse_table_where(sql, verb)
        if not table:
            return None, None
        clause = f" WHERE {where}" if where else ""
        return f"SELECT COUNT(*) FROM {table}{clause}", f"SELECT * FROM {table}{clause} LIMIT 5"
    return None, None


def preview_sqlite(sql: str, db_path: str):
    count_q, preview_q = _preview_queries(sql)
    if not count_q:
        return None, [], []
    try:
        con = sqlite3.connect(db_path)
        try:
            count = con.execute(count_q).fetchone()[0]
            cur = con.execute(preview_q)
            cols = [d[0] for d in cur.description] if cur.description else []
            rows = cur.fetchall()
        finally:
            con.close()
        return count, rows, cols
    except Exception:
        return None, [], []


def preview_postgres(sql: str, url: str):
    if not shutil.which("psql"):
        return None, [], []
    count_q, preview_q = _preview_queries(sql)
    if not count_q:
        return None, [], []
    try:
        count_out = subprocess.run(
            ["psql", url, "-tAc", count_q],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=10,
        ).stdout.strip()
        count = int(count_out) if count_out.lstrip("-").isdigit() else None
        prev = subprocess.run(
            ["psql", url, "-A", "-F", " | ", "-c", preview_q],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=10,
        ).stdout.strip().splitlines()
        cols: List[str] = [prev[0]] if prev else []
        rows = [[r] for r in prev[1:6]] if len(prev) > 1 else []
        return count, rows, cols
    except Exception:
        return None, [], []


def preview(sql: str, target) -> Tuple[Optional[int], list, list]:
    """Dispatch by target kind. `target` is a recovery.Target."""
    if target is None:
        return None, [], []
    if target.kind == "postgres":
        return preview_postgres(sql, target.ref)
    if target.kind == "sqlite":
        return preview_sqlite(sql, target.ref)
    return None, [], []
