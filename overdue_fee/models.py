"""类型定义：收费点明细与账本条目。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class ChargePointDetail:
    """收费点明细。"""

    invoice_id: str
    installment_no: int
    charge_date: date
    remaining_principal: int  # 当日剩余本金（分）
    raw_fee: int              # 原始费用（分）
    actual_fee: int           # 封顶分配后的实际费用（分）
    posted_amount: int        # 账本净入账额（分）


@dataclass(frozen=True)
class LedgerEntry:
    """账本条目（单行，借贷各一行构成一笔分录）。"""

    entry_id: int
    invoice_id: str
    installment_no: int
    charge_date: date
    direction: str   # 'debit' 或 'credit'
    account: str     # 科目名称
    amount: int      # 金额（分，恒为正）
