"""存储层：seekdb / OceanBase（MySQL 协议）为首选，SQLite 为离线降级。

降级不是静默发生的：:class:`Database` 会记录 ``backend`` 与 ``fallback_reason``，
``sddctx doctor`` 与每个上下文的 ``storage`` 字段都会如实打印当前后端，
评审据此可以判断「数据库是否真的参与」。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from sddctx import schema
from sddctx.config import Settings
from sddctx.textutil import now_iso as _now_iso


class DatabaseError(RuntimeError):
    """存储层不可用。"""


@dataclass
class Database:
    dialect: str  # "mysql" | "sqlite"
    backend: str  # "seekdb" | "sqlite"
    label: str  # 人读描述，含版本号
    server_version: str
    database: str
    _conn: Any
    fallback_reason: str = ""
    statements: list[str] = field(default_factory=list)
    queries: list[dict[str, Any]] = field(default_factory=list)

    # ------------------------------------------------------------------ 查询
    def _sql(self, sql: str) -> str:
        if self.dialect == "mysql":
            return sql.replace("?", "%s")
        return sql

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        cur = self._conn.cursor()
        try:
            cur.execute(self._sql(sql), tuple(params))
            rows = cur.fetchall()
        finally:
            cur.close()
        result: list[dict[str, Any]] = []
        for row in rows:
            if isinstance(row, dict):
                result.append(dict(row))
            else:  # sqlite3.Row
                result.append({key: row[key] for key in row.keys()})
        self.queries.append(
            {
                "sql": " ".join(sql.split())[:400],
                "params": list(params),
                "rows": len(result),
            }
        )
        return result

    def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        self.statements.append(sql.strip().split("\n", 1)[0][:160])
        cur = self._conn.cursor()
        try:
            cur.execute(self._sql(sql), tuple(params))
            affected = cur.rowcount
        finally:
            cur.close()
        if self.dialect == "sqlite":
            self._conn.commit()
        return affected

    def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> int:
        rows = list(rows)
        if not rows:
            return 0
        cur = self._conn.cursor()
        try:
            cur.executemany(self._sql(sql), rows)
            affected = cur.rowcount
        finally:
            cur.close()
        if self.dialect == "sqlite":
            self._conn.commit()
        return affected

    def scalar(self, sql: str, params: Sequence[Any] = (), default: Any = 0) -> Any:
        rows = self.query(sql, params)
        if not rows:
            return default
        value = next(iter(rows[0].values()))
        return default if value is None else value

    # ------------------------------------------------------------------ 维护
    def _tables_exist(self) -> bool:
        for table in schema.TABLES:
            try:
                self.query(f"SELECT 1 FROM {table} WHERE 1 = 0")
            except Exception:
                return False
        return True

    def ensure_schema(self, reset: bool = False) -> None:
        """建表。已存在则跳过 DDL——seekdb 的 DDL 延迟在秒级，重复建表会拖慢每次导入。"""
        if not self._tables_exist():
            for statement in schema.ddl(self.dialect):
                self.execute(statement)
        if reset:
            self.reset_data()

    def reset_data(self) -> None:
        """清空数据但保留表结构。

        seekdb 里 DROP/CREATE TABLE 属于 DDL，延迟在秒级且共享 schema 变更队列；
        「重置验证样例」只需要空表，用 DELETE 比反复 DDL 稳得多，也不会等 DDL 超时。
        """
        for table in schema.TABLES:
            self.execute(f"DELETE FROM {table}")

    def drop_all(self) -> None:
        for table in schema.TABLES:
            self.execute(f"DROP TABLE IF EXISTS {table}")

    def put_state(self, key: str, payload: str) -> None:
        self.execute("DELETE FROM pc_state WHERE state_key = ?", (key,))
        self.execute(
            "INSERT INTO pc_state (state_key, payload, updated_at) VALUES (?, ?, ?)",
            (key, payload, _now_iso()),
        )

    def get_state(self, key: str) -> str | None:
        rows = self.query("SELECT payload FROM pc_state WHERE state_key = ?", (key,))
        return rows[0]["payload"] if rows else None

    def table_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for table in schema.TABLES:
            try:
                counts[table] = int(self.scalar(f"SELECT COUNT(*) FROM {table}"))
            except Exception:  # 表可能还没建
                counts[table] = -1
        return counts

    def describe(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "dialect": self.dialect,
            "database": self.database,
            "server_version": self.server_version,
            "label": self.label,
            "fallback_reason": self.fallback_reason,
            "tables": self.table_counts(),
        }

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass


def _open_seekdb(settings: Settings) -> Database:
    try:
        import pymysql
        import pymysql.cursors
    except ImportError as exc:  # pragma: no cover - 依赖缺失
        raise DatabaseError("pymysql is required for seekdb: pip install pymysql") from exc

    try:
        conn = pymysql.connect(
            host=settings.seekdb_host,
            port=settings.seekdb_port,
            user=settings.seekdb_user,
            password=settings.seekdb_password,
            charset="utf8mb4",
            connect_timeout=8,
            autocommit=True,
            cursorclass=pymysql.cursors.DictCursor,
        )
    except Exception as exc:
        raise DatabaseError(f"cannot connect seekdb {settings.seekdb_host}:{settings.seekdb_port}: {exc}") from exc

    cursor = conn.cursor()
    try:
        cursor.execute(
            f"CREATE DATABASE IF NOT EXISTS {settings.seekdb_database} "
            "DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci"
        )
        cursor.execute(f"USE {settings.seekdb_database}")
        # seekdb 默认 ob_query_timeout 只有 10 秒，建索引/全表聚合在慢盘上会被误判成超时
        for statement in (
            "SET SESSION ob_query_timeout = 60000000",
            "SET SESSION ob_trx_timeout = 60000000",
        ):
            try:
                cursor.execute(statement)
            except Exception:  # SQLite/其他兼容实现没有这些变量
                pass
        cursor.execute("SELECT VERSION() AS v")
        version = str(cursor.fetchone()["v"])
    except Exception as exc:
        conn.close()
        raise DatabaseError(f"seekdb bootstrap failed: {exc}") from exc
    finally:
        cursor.close()

    return Database(
        dialect="mysql",
        backend="seekdb",
        label=f"seekdb/MySQL {version}",
        server_version=version,
        database=settings.seekdb_database,
        _conn=conn,
    )


def _open_sqlite(settings: Settings, reason: str = "") -> Database:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.sqlite_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return Database(
        dialect="sqlite",
        backend="sqlite",
        label=f"sqlite3 {sqlite3.sqlite_version}",
        server_version=sqlite3.sqlite_version,
        database=str(settings.sqlite_path),
        _conn=conn,
        fallback_reason=reason,
    )


def open_database(settings: Settings, backend: str | None = None) -> Database:
    """按配置打开数据库；``auto`` 会先试 seekdb，失败后如实降级到 SQLite。"""
    choice = (backend or settings.db_backend or "auto").lower()
    if choice == "seekdb":
        return _open_seekdb(settings)
    if choice == "sqlite":
        return _open_sqlite(settings)
    if choice != "auto":
        raise DatabaseError(f"unknown SDDCTX_DB value: {choice}")
    try:
        return _open_seekdb(settings)
    except DatabaseError as exc:
        return _open_sqlite(settings, reason=str(exc))
