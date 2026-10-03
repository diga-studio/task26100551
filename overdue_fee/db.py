"""SQLite 持久化层：建表与连接管理。"""
from __future__ import annotations

import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS invoices (
    invoice_id   TEXT PRIMARY KEY,
    principal    INTEGER NOT NULL,
    issue_date   TEXT NOT NULL,
    cutoff_date  TEXT
);

CREATE TABLE IF NOT EXISTS repayments (
    repayment_id   TEXT PRIMARY KEY,
    invoice_id     TEXT NOT NULL,
    installment_no INTEGER NOT NULL,
    amount         INTEGER NOT NULL,
    occurred_date  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS charge_points (
    invoice_id     TEXT NOT NULL,
    installment_no INTEGER NOT NULL,
    charge_date    TEXT NOT NULL,
    posted_amount  INTEGER NOT NULL,
    PRIMARY KEY (invoice_id, installment_no, charge_date)
);

CREATE TABLE IF NOT EXISTS ledger (
    entry_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id     TEXT NOT NULL,
    installment_no INTEGER NOT NULL,
    charge_date    TEXT NOT NULL,
    direction      TEXT NOT NULL,
    account        TEXT NOT NULL,
    amount         INTEGER NOT NULL
);
"""


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn
