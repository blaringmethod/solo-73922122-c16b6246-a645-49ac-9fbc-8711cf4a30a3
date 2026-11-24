"""SQLite 连接与建表。单场考试，全部状态存于一个库文件。"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 甲乙卷对照表：卷型 + 题位 + 所选项 -> 题库题号 + 标准选项
CREATE TABLE IF NOT EXISTS mapping (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    paper      TEXT NOT NULL CHECK (paper IN ('A', 'B')),
    pos        TEXT NOT NULL,
    option     TEXT NOT NULL,
    qid        TEXT NOT NULL,
    std_option TEXT NOT NULL,
    UNIQUE (paper, pos, option)
);

-- 每张卡全表共用一个卷型；paper 为 NULL 表示卷型码缺失、待分配
CREATE TABLE IF NOT EXISTS cards (
    card_no  TEXT PRIMARY KEY,
    paper    TEXT CHECK (paper IS NULL OR paper IN ('A', 'B')),
    is_known INTEGER NOT NULL CHECK (is_known IN (0, 1))
);

CREATE TABLE IF NOT EXISTS answers_raw (
    card_no TEXT NOT NULL,
    pos     TEXT NOT NULL,
    option  TEXT NOT NULL,
    PRIMARY KEY (card_no, pos)
);

-- 按（假定）卷型换算后的题库题号 / 标准选项
CREATE TABLE IF NOT EXISTS answers_mapped (
    card_no    TEXT NOT NULL,
    qid        TEXT NOT NULL,
    std_option TEXT NOT NULL,
    PRIMARY KEY (card_no, qid)
);

-- 待供数 CSV：题号、标准选项、人数
CREATE TABLE IF NOT EXISTS supply (
    qid        TEXT NOT NULL,
    std_option TEXT NOT NULL,
    cnt        INTEGER NOT NULL CHECK (cnt >= 0),
    PRIMARY KEY (qid, std_option)
);

-- 当前生效的可行分配（仅登记卷型码缺失的卡）
CREATE TABLE IF NOT EXISTS assignment (
    card_no TEXT PRIMARY KEY,
    paper   TEXT NOT NULL CHECK (paper IN ('A', 'B'))
);

-- 审计日志：更正、核对、发布、暂停、恢复都留痕
CREATE TABLE IF NOT EXISTS audit (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts     TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    event  TEXT NOT NULL,
    detail TEXT NOT NULL
);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


# ---- meta 辅助 ----
META_STATUS = "status"
META_RECEIVED_A = "received_a"
META_EXAM_NAME = "exam_name"

# IMPORTED  已导入作答与对照表，尚未核对出可行分配
# VERIFIED  已核对出可行分配（未发布）
# SUPPLYING 已发布供数
# PAUSED    更正后现用分配失效，已暂停供数
STATUSES = ("IMPORTED", "VERIFIED", "SUPPLYING", "PAUSED")


def get_meta(conn: sqlite3.Connection, key: str, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )


def get_status(conn: sqlite3.Connection) -> str:
    return get_meta(conn, META_STATUS, "IMPORTED")


def audit(conn: sqlite3.Connection, event: str, detail: str) -> None:
    conn.execute("INSERT INTO audit(event, detail) VALUES(?, ?)", (event, detail))
