"""SQLite 持久化层：建表与连接管理。"""

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS invoices (
    invoice_id   TEXT PRIMARY KEY,
    principal    INTEGER NOT NULL,          -- 原始本金，整数分
    issue_date   TEXT NOT NULL              -- 开票日 ISO 日期
);

CREATE TABLE IF NOT EXISTS repayments (
    repayment_id   TEXT PRIMARY KEY,
    invoice_id     TEXT NOT NULL,
    installment_no INTEGER NOT NULL,        -- 归属期号，不可更改
    amount         INTEGER NOT NULL,        -- 金额（分），0 表示已撤销
    repay_date     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS billing_state (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    as_of_date  TEXT                        -- 计费截至日，NULL 表示未推进过
);

CREATE TABLE IF NOT EXISTS ledger (
    entry_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id      TEXT NOT NULL,
    installment_no  INTEGER NOT NULL,
    charge_date     TEXT NOT NULL,          -- 收费点日期
    direction       TEXT NOT NULL CHECK (direction IN ('charge', 'reversal')),
    amount          INTEGER NOT NULL,       -- 永远为正数
    debit_account   TEXT NOT NULL,
    credit_account  TEXT NOT NULL
);
"""

ACCOUNTS_RECEIVABLE = "应收逾期费"
FEE_INCOME = "费用收入"


def connect(path: str | Path) -> sqlite3.Connection:
    """打开（必要时创建）数据库并初始化表结构。"""
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT OR IGNORE INTO billing_state (id, as_of_date) VALUES (1, NULL)"
    )
    return conn
