"""逾期费核算核心服务。

四类能力：
- 登记发票
- 登记或更正还款（同编号不同金额/日期为更正，金额为 0 为撤销）
- 推进计费截至日（只能向后）
- 查询收费点明细与费用账本
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from .db import ACCOUNTS_RECEIVABLE, FEE_INCOME, connect

INSTALLMENT_COUNT = 6
INSTALLMENT_PERIOD_DAYS = 30
FIRST_CHARGE_OFFSET_DAYS = 8
CHARGE_INTERVAL_DAYS = 30
FEE_RATE_PERMILLE = 15      # 1.5%
CAP_RATE_PERMILLE = 150     # 15%


def _round_permille(amount: int, permille: int) -> int:
    """amount * permille / 1000，四舍五入到整数分（amount 非负）。"""
    quotient, remainder = divmod(amount * permille, 1000)
    if remainder * 2 >= 1000:
        quotient += 1
    return quotient


def _installment_principal(principal: int, installment_no: int) -> int:
    """本金平均分配，余数从第 1 期开始逐期增加 1 分。"""
    base, remainder = divmod(principal, INSTALLMENT_COUNT)
    return base + (1 if installment_no <= remainder else 0)


def _parse_day(value: str) -> date:
    return date.fromisoformat(value)


@dataclass(frozen=True)
class ChargePoint:
    """一个收费点的明细。"""

    installment_no: int
    charge_date: date
    raw_fee: int        # 按最新还款事实计算的原始费用（分）
    net_fee: int        # 账本净入账额（分）
    capped: bool        # 是否因封顶被削减


@dataclass(frozen=True)
class LedgerEntry:
    """一条账本记录（只能追加，不可修改）。"""

    entry_id: int
    invoice_id: str
    installment_no: int
    charge_date: date
    direction: str      # 'charge' 或 'reversal'
    amount: int
    debit_account: str
    credit_account: str


class OverdueFeeService:
    def __init__(self, db_path: str | Path):
        self._conn: sqlite3.Connection = connect(db_path)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "OverdueFeeService":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def register_invoice(self, invoice_id: str, principal: int, issue_date: date) -> None:
        if principal < 0:
            raise ValueError("principal 必须非负")
        with self._conn:
            self._conn.execute(
                "INSERT INTO invoices (invoice_id, principal, issue_date)"
                " VALUES (?, ?, ?)",
                (invoice_id, principal, issue_date.isoformat()),
            )
            self._sync_invoice(invoice_id)

    def submit_repayment(
        self,
        repayment_id: str,
        invoice_id: str,
        installment_no: int,
        amount: int,
        repay_date: date,
    ) -> None:
        if amount < 0:
            raise ValueError("amount 必须非负")
        if not 1 <= installment_no <= INSTALLMENT_COUNT:
            raise ValueError(f"installment_no 必须在 1..{INSTALLMENT_COUNT}")
        with self._conn:
            row = self._conn.execute(
                "SELECT invoice_id, installment_no, amount, repay_date"
                " FROM repayments WHERE repayment_id = ?",
                (repayment_id,),
            ).fetchone()
            if row is None:
                self._require_invoice(invoice_id)
                self._conn.execute(
                    "INSERT INTO repayments"
                    " (repayment_id, invoice_id, installment_no, amount, repay_date)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (repayment_id, invoice_id, installment_no, amount,
                     repay_date.isoformat()),
                )
            else:
                if row["invoice_id"] != invoice_id or row["installment_no"] != installment_no:
                    raise ValueError("还款归属（发票/期号）不可更改")
                if row["amount"] == amount and row["repay_date"] == repay_date.isoformat():
                    return  # 重复提交相同内容，不产生新账目
                self._conn.execute(
                    "UPDATE repayments SET amount = ?, repay_date = ?"
                    " WHERE repayment_id = ?",
                    (amount, repay_date.isoformat(), repayment_id),
                )
            self._sync_invoice(invoice_id)

    def advance_cutoff(self, new_date: date) -> None:
        with self._conn:
            row = self._conn.execute(
                "SELECT as_of_date FROM billing_state WHERE id = 1"
            ).fetchone()
            current = row["as_of_date"]
            if current is not None and new_date.isoformat() < current:
                raise ValueError("计费截至日只能向后推进")
            if current == new_date.isoformat():
                return
            self._conn.execute(
                "UPDATE billing_state SET as_of_date = ? WHERE id = 1",
                (new_date.isoformat(),),
            )
            for inv in self._conn.execute("SELECT invoice_id FROM invoices"):
                self._sync_invoice(inv["invoice_id"])

    def charge_points(self, invoice_id: str) -> list[ChargePoint]:
        return [
            ChargePoint(
                installment_no=no,
                charge_date=charge_date,
                raw_fee=raw_fee,
                net_fee=net_fee,
                capped=net_fee < raw_fee,
            )
            for no, charge_date, raw_fee, net_fee in self._expected_points(invoice_id)
        ]

    def ledger_entries(self, invoice_id: str | None = None) -> list[LedgerEntry]:
        sql = (
            "SELECT entry_id, invoice_id, installment_no, charge_date,"
            " direction, amount, debit_account, credit_account FROM ledger"
        )
        params: tuple = ()
        if invoice_id is not None:
            sql += " WHERE invoice_id = ?"
            params = (invoice_id,)
        sql += " ORDER BY entry_id"
        return [
            LedgerEntry(
                entry_id=row["entry_id"],
                invoice_id=row["invoice_id"],
                installment_no=row["installment_no"],
                charge_date=_parse_day(row["charge_date"]),
                direction=row["direction"],
                amount=row["amount"],
                debit_account=row["debit_account"],
                credit_account=row["credit_account"],
            )
            for row in self._conn.execute(sql, params)
        ]

    # ---- 内部实现 ----

    def _require_invoice(self, invoice_id: str) -> sqlite3.Row:
        row = self._conn.execute(
            "SELECT invoice_id, principal, issue_date FROM invoices"
            " WHERE invoice_id = ?",
            (invoice_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"发票不存在: {invoice_id}")
        return row

    def _cutoff(self) -> date | None:
        row = self._conn.execute(
            "SELECT as_of_date FROM billing_state WHERE id = 1"
        ).fetchone()
        return _parse_day(row["as_of_date"]) if row["as_of_date"] else None

    def _expected_points(self, invoice_id: str) -> list[tuple[int, date, int, int]]:
        """按最新还款事实计算收费点：(期号, 收费日, 原始费用, 封顶后费用)。

        按 (收费日, 期号) 升序分配封顶额度。
        """
        invoice = self._require_invoice(invoice_id)
        as_of = self._cutoff()
        if as_of is None:
            return []
        principal = invoice["principal"]
        issue_date = _parse_day(invoice["issue_date"])

        repayments: dict[int, list[tuple[date, int]]] = {
            no: [] for no in range(1, INSTALLMENT_COUNT + 1)
        }
        for row in self._conn.execute(
            "SELECT installment_no, amount, repay_date FROM repayments"
            " WHERE invoice_id = ? AND amount > 0",
            (invoice_id,),
        ):
            repayments[row["installment_no"]].append(
                (_parse_day(row["repay_date"]), row["amount"])
            )

        points: list[tuple[date, int, int]] = []  # (收费日, 期号, 原始费用)
        for no in range(1, INSTALLMENT_COUNT + 1):
            due = issue_date + timedelta(days=INSTALLMENT_PERIOD_DAYS * no)
            inst_principal = _installment_principal(principal, no)
            paid = sorted(repayments[no])
            charge_day = due + timedelta(days=FIRST_CHARGE_OFFSET_DAYS)
            while charge_day <= as_of:
                # 同日还款先计入，再收费
                remaining = inst_principal - sum(
                    amt for day, amt in paid if day <= charge_day
                )
                points.append(
                    (charge_day, no, _round_permille(remaining, FEE_RATE_PERMILLE))
                )
                charge_day += timedelta(days=CHARGE_INTERVAL_DAYS)

        points.sort(key=lambda p: (p[0], p[1]))
        cap_left = _round_permille(principal, CAP_RATE_PERMILLE)
        result = []
        for charge_day, no, raw_fee in points:
            net_fee = min(raw_fee, cap_left)
            cap_left -= net_fee
            result.append((no, charge_day, raw_fee, net_fee))
        return result

    def _sync_invoice(self, invoice_id: str) -> None:
        """修正截至日内所有收费点，使净入账额等于最新计算费用。"""
        expected = self._expected_points(invoice_id)
        booked: dict[tuple[int, str], int] = {}
        for row in self._conn.execute(
            "SELECT installment_no, charge_date, direction, amount FROM ledger"
            " WHERE invoice_id = ?",
            (invoice_id,),
        ):
            key = (row["installment_no"], row["charge_date"])
            delta = row["amount"] if row["direction"] == "charge" else -row["amount"]
            booked[key] = booked.get(key, 0) + delta

        for no, charge_day, _raw, net_fee in expected:
            key = (no, charge_day.isoformat())
            diff = net_fee - booked.get(key, 0)
            if diff > 0:
                self._post(invoice_id, no, key[1], "charge", diff)
            elif diff < 0:
                self._post(invoice_id, no, key[1], "reversal", -diff)

    def _post(
        self,
        invoice_id: str,
        installment_no: int,
        charge_date: str,
        direction: str,
        amount: int,
    ) -> None:
        if direction == "charge":
            debit, credit = ACCOUNTS_RECEIVABLE, FEE_INCOME
        else:
            debit, credit = FEE_INCOME, ACCOUNTS_RECEIVABLE
        self._conn.execute(
            "INSERT INTO ledger"
            " (invoice_id, installment_no, charge_date, direction, amount,"
            "  debit_account, credit_account)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (invoice_id, installment_no, charge_date, direction, amount,
             debit, credit),
        )
