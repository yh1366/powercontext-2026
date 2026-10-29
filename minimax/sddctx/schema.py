"""建表语句：同一份逻辑模型，两份方言（seekdb/MySQL、SQLite）。

设计取舍（写进 docs/design.md）：不使用 FULLTEXT / 向量索引，而是把
「词 → 块」的倒排表 ``pc_postings`` 与文档频率表 ``pc_df`` 显式落库，
这样 BM25 的候选召回与统计量都由数据库查询产生，检索过程可审计、可复现，
也不依赖任何需要额外插件的索引能力。
"""

from __future__ import annotations

TABLES = (
    "pc_documents",
    "pc_chunks",
    "pc_postings",
    "pc_df",
    "pc_runs",
    "pc_state",
)

_MYSQL = [
    """
    CREATE TABLE IF NOT EXISTS pc_documents (
        doc_id VARCHAR(48) NOT NULL,
        category VARCHAR(16) NOT NULL,
        path VARCHAR(512) NOT NULL,
        title VARCHAR(512) NOT NULL,
        spec_version VARCHAR(32) NOT NULL DEFAULT '',
        content_sha VARCHAR(64) NOT NULL,
        ingested_at VARCHAR(32) NOT NULL,
        PRIMARY KEY (doc_id),
        KEY idx_pc_documents_category (category)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS pc_chunks (
        chunk_id VARCHAR(48) NOT NULL,
        doc_id VARCHAR(48) NOT NULL,
        category VARCHAR(16) NOT NULL,
        path VARCHAR(512) NOT NULL,
        symbol VARCHAR(256) NOT NULL,
        heading VARCHAR(512) NOT NULL,
        start_line INT NOT NULL,
        end_line INT NOT NULL,
        token_len INT NOT NULL,
        content LONGTEXT NOT NULL,
        PRIMARY KEY (chunk_id),
        KEY idx_pc_chunks_doc (doc_id),
        KEY idx_pc_chunks_category (category)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS pc_postings (
        term VARCHAR(120) NOT NULL,
        chunk_id VARCHAR(48) NOT NULL,
        tf INT NOT NULL,
        PRIMARY KEY (term, chunk_id),
        KEY idx_pc_postings_chunk (chunk_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS pc_df (
        term VARCHAR(120) NOT NULL,
        df INT NOT NULL,
        PRIMARY KEY (term)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS pc_runs (
        run_id VARCHAR(64) NOT NULL,
        task_id VARCHAR(64) NOT NULL,
        created_at VARCHAR(32) NOT NULL,
        backend VARCHAR(32) NOT NULL,
        payload LONGTEXT NOT NULL,
        PRIMARY KEY (run_id),
        KEY idx_pc_runs_task (task_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS pc_state (
        state_key VARCHAR(191) NOT NULL,
        payload LONGTEXT NOT NULL,
        updated_at VARCHAR(32) NOT NULL,
        PRIMARY KEY (state_key)
    )
    """,
]

_SQLITE = [
    """
    CREATE TABLE IF NOT EXISTS pc_documents (
        doc_id TEXT PRIMARY KEY,
        category TEXT NOT NULL,
        path TEXT NOT NULL,
        title TEXT NOT NULL,
        spec_version TEXT NOT NULL DEFAULT '',
        content_sha TEXT NOT NULL,
        ingested_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS pc_chunks (
        chunk_id TEXT PRIMARY KEY,
        doc_id TEXT NOT NULL,
        category TEXT NOT NULL,
        path TEXT NOT NULL,
        symbol TEXT NOT NULL,
        heading TEXT NOT NULL,
        start_line INTEGER NOT NULL,
        end_line INTEGER NOT NULL,
        token_len INTEGER NOT NULL,
        content TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS pc_postings (
        term TEXT NOT NULL,
        chunk_id TEXT NOT NULL,
        tf INTEGER NOT NULL,
        PRIMARY KEY (term, chunk_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS pc_df (
        term TEXT PRIMARY KEY,
        df INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS pc_runs (
        run_id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL,
        created_at TEXT NOT NULL,
        backend TEXT NOT NULL,
        payload TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS pc_state (
        state_key TEXT PRIMARY KEY,
        payload TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
]

_SQLITE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_pc_documents_category ON pc_documents (category)",
    "CREATE INDEX IF NOT EXISTS idx_pc_chunks_doc ON pc_chunks (doc_id)",
    "CREATE INDEX IF NOT EXISTS idx_pc_chunks_category ON pc_chunks (category)",
    "CREATE INDEX IF NOT EXISTS idx_pc_postings_chunk ON pc_postings (chunk_id)",
    "CREATE INDEX IF NOT EXISTS idx_pc_runs_task ON pc_runs (task_id)",
]


def ddl(dialect: str) -> list[str]:
    if dialect == "mysql":
        return list(_MYSQL)
    if dialect == "sqlite":
        return list(_SQLITE) + _SQLITE_INDEXES
    raise ValueError(f"unsupported dialect: {dialect}")
