"""分期融资逾期费核算服务。"""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta

from .db import connect
from .models import ChargePointDetail, LedgerEntry

INSTALLMENT_COUNT = 6
INSTALLMENT_INTERVAL_DAYS = 30
FIRST_CHARGE_DELAY_DAYS = 8
CHARGE_INTERVAL_DAYS = 30
RATE_PERMILLE = 15          # 1.5%
CAP_RATE_PERMILLE = 150     # 15%

ACCOUNT_RECEIVABLE = "应收逾期费"
ACCOUNT_INCOME = "费用收入"


def _as_date(value: date | str) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(value)


def _round_half_up(numerator: int, denominator: int) -> int:
    """非负整数分数的四舍五入。"""
    return (2 * numerator + denominator) // (2 * denominator)


def _installment_principal(principal: int, installment_no: int) -> int:
    """本金平均分配，余数从第1期开始逐期多1分。"""
    base, remainder = divmod(principal, INSTALLMENT_COUNT)
    return base + (1 if installment_no <= remainder else 0)


class OverdueFeeService:
    """逾期费核算入口，由上层直接调用。"""

    def __init__(self, db_path: str) -> None:
        self._conn = connect(db_path)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "OverdueFeeService":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---- 登记发票 ----

    def register_invoice(self, invoice_id: str, principal: int, issue_date: date | str) -> None:
        if principal <= 0:
            raise ValueError("principal 必须为正整数（分）")
        try:
            self._conn.execute(
                "INSERT INTO invoices (invoice_id, principal, issue_date, cutoff_date)"
                " VALUES (?, ?, ?, NULL)",
                (invoice_id, principal, _as_date(issue_date).isoformat()),
            )
        except sqlite3.IntegrityError:
            raise ValueError(f"发票已存在: {invoice_id}") from None
        self._conn.commit()

    # ---- 登记或更正还款 ----

    def submit_repayment(
        self,
        repayment_id: str,
        invoice_id: str,
        installment_no: int,
        amount: int,
        occurred_date: date | str,
    ) -> None:
        if amount < 0:
            raise ValueError("amount 不能为负")
        if not 1 <= installment_no <= INSTALLMENT_COUNT:
            raise ValueError("installment_no 必须在 1..6")
        occurred = _as_date(occurred_date)
        row = self._conn.execute(
            "SELECT invoice_id, installment_no, amount, occurred_date"
            " FROM repayments WHERE repayment_id = ?",
            (repayment_id,),
        ).fetchone()
        if row is not None:
            if row["invoice_id"] != invoice_id or row["installment_no"] != installment_no:
                raise ValueError("还款归属不可更改")
            if row["amount"] == amount and row["occurred_date"] == occurred.isoformat():
                return  # 重复提交相同内容，不产生新账目
            if amount == 0:
                self._conn.execute(
                    "DELETE FROM repayments WHERE repayment_id = ?", (repayment_id,)
                )
            else:
                self._conn.execute(
                    "UPDATE repayments SET amount = ?, occurred_date = ?"
                    " WHERE repayment_id = ?",
                    (amount, occurred.isoformat(), repayment_id),
                )
        else:
            self._require_invoice(invoice_id)
            if amount == 0:
                return  # 对不存在的还款执行撤销，视为无操作
            self._conn.execute(
                "INSERT INTO repayments"
                " (repayment_id, invoice_id, installment_no, amount, occurred_date)"
                " VALUES (?, ?, ?, ?, ?)",
                (repayment_id, invoice_id, installment_no, amount, occurred.isoformat()),
            )
        self._recompute(invoice_id)
        self._conn.commit()

    # ---- 推进计费截至日 ----

    def advance_cutoff(self, invoice_id: str, new_cutoff: date | str) -> None:
        inv = self._require_invoice(invoice_id)
        new_cutoff = _as_date(new_cutoff)
        current = inv["cutoff_date"]
        if current is not None and new_cutoff < date.fromisoformat(current):
            raise ValueError("截至日只能向后推进")
        if current is not None and new_cutoff == date.fromisoformat(current):
            return
        self._conn.execute(
            "UPDATE invoices SET cutoff_date = ? WHERE invoice_id = ?",
            (new_cutoff.isoformat(), invoice_id),
        )
        self._recompute(invoice_id)
        self._conn.commit()

    # ---- 查询 ----

    def charge_points(self, invoice_id: str) -> list[ChargePointDetail]:
        inv = self._require_invoice(invoice_id)
        points = self._compute_points(inv)
        posted = self._posted_amounts(invoice_id)
        return [
            ChargePointDetail(
                invoice_id=invoice_id,
                installment_no=p["installment_no"],
                charge_date=p["charge_date"],
                remaining_principal=p["remaining"],
                raw_fee=p["raw_fee"],
                actual_fee=p["actual_fee"],
                posted_amount=posted.get(
                    (p["installment_no"], p["charge_date"].isoformat()), 0
                ),
            )
            for p in points
        ]

    def ledger(self, invoice_id: str) -> list[LedgerEntry]:
        rows = self._conn.execute(
            "SELECT entry_id, invoice_id, installment_no, charge_date,"
            " direction, account, amount FROM ledger"
            " WHERE invoice_id = ? ORDER BY entry_id",
            (invoice_id,),
        ).fetchall()
        return [
            LedgerEntry(
                entry_id=r["entry_id"],
                invoice_id=r["invoice_id"],
                installment_no=r["installment_no"],
                charge_date=date.fromisoformat(r["charge_date"]),
                direction=r["direction"],
                account=r["account"],
                amount=r["amount"],
            )
            for r in rows
        ]

    # ---- 内部 ----

    def _require_invoice(self, invoice_id: str) -> sqlite3.Row:
        row = self._conn.execute(
            "SELECT invoice_id, principal, issue_date, cutoff_date"
            " FROM invoices WHERE invoice_id = ?",
            (invoice_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"发票不存在: {invoice_id}")
        return row

    def _posted_amounts(self, invoice_id: str) -> dict[tuple[int, str], int]:
        rows = self._conn.execute(
            "SELECT installment_no, charge_date, posted_amount FROM charge_points"
            " WHERE invoice_id = ?",
            (invoice_id,),
        ).fetchall()
        return {(r["installment_no"], r["charge_date"]): r["posted_amount"] for r in rows}

    def _compute_points(self, inv: sqlite3.Row) -> list[dict]:
        """按最新还款事实计算截至日内全部收费点。"""
        if inv["cutoff_date"] is None:
            return []
        cutoff = date.fromisoformat(inv["cutoff_date"])
        issue_date = date.fromisoformat(inv["issue_date"])
        principal = inv["principal"]

        repay_rows = self._conn.execute(
            "SELECT installment_no, amount, occurred_date FROM repayments"
            " WHERE invoice_id = ? ORDER BY occurred_date",
            (inv["invoice_id"],),
        ).fetchall()
        repaid: dict[int, list[tuple[date, int]]] = {}
        for r in repay_rows:
            repaid.setdefault(r["installment_no"], []).append(
                (date.fromisoformat(r["occurred_date"]), r["amount"])
            )

        points: list[dict] = []
        for no in range(1, INSTALLMENT_COUNT + 1):
            due = issue_date + timedelta(days=INSTALLMENT_INTERVAL_DAYS * no)
            charge_date = due + timedelta(days=FIRST_CHARGE_DELAY_DAYS)
            share = _installment_principal(principal, no)
            while charge_date <= cutoff:
                # 同日还款先计入，再收费
                paid = sum(
                    amt for d, amt in repaid.get(no, []) if d <= charge_date
                )
                remaining = share - paid
                if remaining < 0:
                    raise ValueError("累计还款超过该期本金")
                points.append(
                    {
                        "installment_no": no,
                        "charge_date": charge_date,
                        "remaining": remaining,
                        "raw_fee": _round_half_up(remaining * RATE_PERMILLE, 1000),
                    }
                )
                charge_date += timedelta(days=CHARGE_INTERVAL_DAYS)

        # 同日多个收费点按期号升序；按顺序分配封顶额度
        points.sort(key=lambda p: (p["charge_date"], p["installment_no"]))
        cap_remaining = _round_half_up(principal * CAP_RATE_PERMILLE, 1000)
        for p in points:
            p["actual_fee"] = min(p["raw_fee"], cap_remaining)
            cap_remaining -= p["actual_fee"]
        return points

    def _recompute(self, invoice_id: str) -> None:
        """重算收费点并对差异追加补记/冲回分录，旧账目保持不变。"""
        inv = self._require_invoice(invoice_id)
        points = self._compute_points(inv)
        posted = self._posted_amounts(invoice_id)
        for p in points:
            key = (p["installment_no"], p["charge_date"].isoformat())
            old = posted.get(key, 0)
            delta = p["actual_fee"] - old
            if delta > 0:
                self._append_entry(invoice_id, p, ACCOUNT_RECEIVABLE, ACCOUNT_INCOME, delta)
            elif delta < 0:
                self._append_entry(invoice_id, p, ACCOUNT_INCOME, ACCOUNT_RECEIVABLE, -delta)
            if delta != 0 or key not in posted:
                self._conn.execute(
                    "INSERT INTO charge_points"
                    " (invoice_id, installment_no, charge_date, posted_amount)"
                    " VALUES (?, ?, ?, ?)"
                    " ON CONFLICT (invoice_id, installment_no, charge_date)"
                    " DO UPDATE SET posted_amount = excluded.posted_amount",
                    (invoice_id, p["installment_no"], key[1], p["actual_fee"]),
                )

    def _append_entry(
        self,
        invoice_id: str,
        point: dict,
        debit_account: str,
        credit_account: str,
        amount: int,
    ) -> None:
        charge_date = point["charge_date"].isoformat()
        no = point["installment_no"]
        self._conn.execute(
            "INSERT INTO ledger"
            " (invoice_id, installment_no, charge_date, direction, account, amount)"
            " VALUES (?, ?, ?, 'debit', ?, ?)",
            (invoice_id, no, charge_date, debit_account, amount),
        )
        self._conn.execute(
            "INSERT INTO ledger"
            " (invoice_id, installment_no, charge_date, direction, account, amount)"
            " VALUES (?, ?, ?, 'credit', ?, ?)",
            (invoice_id, no, charge_date, credit_account, amount),
        )
