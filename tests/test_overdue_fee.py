"""逾期费核算库的自动化测试。

测试内包含一个独立于被测实现的参考计算函数，用于对照收费点结果。
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from overdue_fee import OverdueFeeService

INSTALLMENTS = 6


def round_half_up(numerator: int, denominator: int) -> int:
    return (2 * numerator + denominator) // (2 * denominator)


def reference_points(principal, issue_date, repayments, cutoff):
    """按题述规则独立重算收费点。

    repayments: [(installment_no, amount, occurred_date)]，仅含未撤销还款。
    返回 [(charge_date, installment_no, remaining, raw_fee, actual_fee)]。
    """
    if cutoff is None:
        return []
    base, rem = divmod(principal, INSTALLMENTS)
    points = []
    for no in range(1, INSTALLMENTS + 1):
        share = base + (1 if no <= rem else 0)
        due = issue_date + timedelta(days=30 * no)
        charge = due + timedelta(days=8)
        while charge <= cutoff:
            paid = sum(
                amt for n, amt, d in repayments if n == no and d <= charge
            )
            remaining = share - paid
            points.append((charge, no, remaining, round_half_up(remaining * 15, 1000)))
            charge += timedelta(days=30)
    points.sort(key=lambda p: (p[0], p[1]))
    cap = round_half_up(principal * 150, 1000)
    result = []
    for charge, no, remaining, raw in points:
        actual = min(raw, cap)
        cap -= actual
        result.append((charge, no, remaining, raw, actual))
    return result


@pytest.fixture()
def svc(tmp_path):
    service = OverdueFeeService(str(tmp_path / "fee.db"))
    yield service
    service.close()


def assert_state(svc, invoice_id, principal, issue_date, repayments, cutoff):
    """对照参考实现，并验证净入账、借贷平衡。"""
    expected = reference_points(principal, issue_date, repayments, cutoff)
    points = svc.charge_points(invoice_id)
    got = [
        (p.charge_date, p.installment_no, p.remaining_principal, p.raw_fee, p.actual_fee)
        for p in points
    ]
    assert got == expected
    # 每点净入账额必须等于最新事实计算的费用
    for p in points:
        assert p.posted_amount == p.actual_fee
    # 账本按收费点汇总必须等于净入账额，且整体借贷平衡
    ledger = svc.ledger(invoice_id)
    net: dict[tuple[date, int], int] = {}
    debit_total = credit_total = 0
    for e in ledger:
        assert e.amount > 0
        if e.account == "应收逾期费":
            signed = e.amount if e.direction == "debit" else -e.amount
            net[(e.charge_date, e.installment_no)] = (
                net.get((e.charge_date, e.installment_no), 0) + signed
            )
        if e.direction == "debit":
            debit_total += e.amount
        else:
            credit_total += e.amount
    assert debit_total == credit_total
    for p in points:
        key = (p.charge_date, p.installment_no)
        assert net.get(key, 0) == p.actual_fee
    return ledger


def test_mixed_history_with_corrections_and_revocation(svc):
    principal = 100003  # 余数 1：第 1 期 16668，其余各 16667
    issue = date(2026, 1, 1)
    svc.register_invoice("INV1", principal, issue)
    repayments: list[tuple[int, int, date]] = []

    # 推进截至日：仅第 1 期第一个收费点（2026-02-08）入账
    svc.advance_cutoff("INV1", date(2026, 2, 20))
    ledger = assert_state(svc, "INV1", principal, issue, repayments, date(2026, 2, 20))
    assert len(ledger) == 2  # 一个收费点一笔分录，借贷各一行，费用 250 分

    # 补录还款：与收费点同日，先计入再收费
    svc.submit_repayment("R1", "INV1", 1, 10000, date(2026, 2, 8))
    repayments = [(1, 10000, date(2026, 2, 8))]
    prev = assert_state(svc, "INV1", principal, issue, repayments, date(2026, 2, 20))
    assert len(prev) == len(ledger) + 2  # 冲回 150
    assert prev[: len(ledger)] == ledger  # 原账目未变

    # 更正金额与日期
    svc.submit_repayment("R1", "INV1", 1, 5000, date(2026, 2, 1))
    repayments = [(1, 5000, date(2026, 2, 1))]
    prev2 = assert_state(svc, "INV1", principal, issue, repayments, date(2026, 2, 20))
    assert prev2[: len(prev)] == prev

    # 相同请求重试不产生新账目
    svc.submit_repayment("R1", "INV1", 1, 5000, date(2026, 2, 1))
    assert svc.ledger("INV1") == prev2

    # 推进截至日，出现更多收费点（含同日多期）
    svc.advance_cutoff("INV1", date(2026, 4, 15))
    prev3 = assert_state(svc, "INV1", principal, issue, repayments, date(2026, 4, 15))
    assert prev3[: len(prev2)] == prev2

    # 第 2 期全额还款（收费点当日），随后撤销
    svc.submit_repayment("R2", "INV1", 2, 16667, date(2026, 3, 10))
    repayments.append((2, 16667, date(2026, 3, 10)))
    prev4 = assert_state(svc, "INV1", principal, issue, repayments, date(2026, 4, 15))
    svc.submit_repayment("R2", "INV1", 2, 0, date(2026, 3, 10))
    repayments = repayments[:1]
    prev5 = assert_state(svc, "INV1", principal, issue, repayments, date(2026, 4, 15))
    assert prev5[: len(prev4)] == prev4

    # 更正还款日期到更晚的收费点当日
    svc.submit_repayment("R1", "INV1", 1, 5000, date(2026, 3, 10))
    repayments = [(1, 5000, date(2026, 3, 10))]
    assert_state(svc, "INV1", principal, issue, repayments, date(2026, 4, 15))

    # 归属不可更改 / 截至日不可倒退
    with pytest.raises(ValueError):
        svc.submit_repayment("R1", "INV1", 2, 5000, date(2026, 3, 10))
    with pytest.raises(ValueError):
        svc.advance_cutoff("INV1", date(2026, 4, 1))


def test_cap_release_after_backfill_and_restore_on_revoke(svc):
    principal = 600000  # 每期 100000，单点原始费 1500，封顶 90000（60 个点）
    issue = date(2026, 1, 1)
    svc.register_invoice("INV2", principal, issue)
    cutoff = date(2028, 1, 1)
    svc.advance_cutoff("INV2", cutoff)

    points = svc.charge_points("INV2")
    charged = [p for p in points if p.actual_fee > 0]
    assert len(charged) == 60
    assert sum(p.actual_fee for p in points) == 90000
    assert_state(svc, "INV2", principal, issue, [], cutoff)
    ledger_before = svc.ledger("INV2")

    # 已封顶后补录早期还款：第 1 期在首个收费点前还 50000
    svc.submit_repayment("R9", "INV2", 1, 50000, date(2026, 2, 1))
    repayments = [(1, 50000, date(2026, 2, 1))]
    points_after = svc.charge_points("INV2")
    inst1_points = [p for p in points_after if p.installment_no == 1]
    assert all(p.raw_fee == 750 for p in inst1_points)  # 剩余 50000 的 1.5%
    # 总额仍封顶 90000，但额度释放给了后续点
    assert sum(p.actual_fee for p in points_after) == 90000
    newly_charged = [p for p in points_after if p.actual_fee > 0]
    # 第 1 期各点省 750，释放额度让此前被封顶阻挡的后续点重新收费
    assert len(newly_charged) == 67
    assert len(newly_charged) > 60
    ledger_after = assert_state(svc, "INV2", principal, issue, repayments, cutoff)
    assert ledger_after[: len(ledger_before)] == ledger_before
    # 早期点发生冲回（借：费用收入）
    reversals = [
        e for e in ledger_after[len(ledger_before):]
        if e.direction == "debit" and e.account == "费用收入"
    ]
    assert reversals and all(e.installment_no == 1 for e in reversals)

    # 撤销该还款：逐点结果恢复
    svc.submit_repayment("R9", "INV2", 1, 0, date(2026, 2, 1))
    points_restored = svc.charge_points("INV2")
    assert [
        (p.charge_date, p.installment_no, p.actual_fee, p.posted_amount)
        for p in points_restored
    ] == [(p.charge_date, p.installment_no, p.actual_fee, p.posted_amount) for p in points]
    final_ledger = assert_state(svc, "INV2", principal, issue, [], cutoff)
    assert final_ledger[: len(ledger_after)] == ledger_after
