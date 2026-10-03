"""逾期费核算库的自动化测试。"""

from dataclasses import astuple
from datetime import date, timedelta

import pytest

from overdue_fees import OverdueFeeService
from overdue_fees.db import ACCOUNTS_RECEIVABLE, FEE_INCOME


@pytest.fixture
def service(tmp_path):
    svc = OverdueFeeService(tmp_path / "fees.db")
    yield svc
    svc.close()


def round_half_up_milli(amount: int, permille: int) -> int:
    q, r = divmod(amount * permille, 1000)
    return q + (1 if r * 2 >= 1000 else 0)


def reference_points(principal, issue: date, repayments, as_of: date):
    """独立参照模型：返回 [(期号, 收费日, 原始费用, 封顶后费用)]，按收费顺序。"""
    if as_of is None:
        return []
    base, rem = divmod(principal, 6)
    points = []
    for no in range(1, 7):
        inst_principal = base + (1 if no <= rem else 0)
        due = issue + timedelta(days=30 * no)
        day = due + timedelta(days=8)
        while day <= as_of:
            paid = sum(a for d, a in repayments.get(no, []) if d <= day)
            points.append((day, no, round_half_up_milli(inst_principal - paid, 15)))
            day += timedelta(days=30)
    points.sort(key=lambda p: (p[0], p[1]))
    cap_left = round_half_up_milli(principal, 150)
    result = []
    for day, no, raw in points:
        net = min(raw, cap_left)
        cap_left -= net
        result.append((no, day, raw, net))
    return result


def net_by_point(entries):
    net = {}
    for e in entries:
        key = (e.installment_no, e.charge_date)
        net[key] = net.get(key, 0) + (e.amount if e.direction == "charge" else -e.amount)
    return net


def assert_ledger_sound(entries):
    """借贷平衡且科目方向正确。"""
    for e in entries:
        assert e.amount > 0
        if e.direction == "charge":
            assert (e.debit_account, e.credit_account) == (ACCOUNTS_RECEIVABLE, FEE_INCOME)
        else:
            assert (e.debit_account, e.credit_account) == (FEE_INCOME, ACCOUNTS_RECEIVABLE)


def assert_points_match(service, invoice_id, expected):
    points = service.charge_points(invoice_id)
    got = [(p.installment_no, p.charge_date, p.raw_fee, p.net_fee) for p in points]
    assert got == expected
    # 账本净入账额 == 最新还款事实计算的费用
    net = net_by_point(service.ledger_entries(invoice_id))
    for no, day, _raw, fee in expected:
        assert net.get((no, day), 0) == fee


def snapshot(service):
    return [astuple(e) for e in service.ledger_entries()]


def test_mixed_history(service):
    inv = "INV-1"
    service.register_invoice(inv, 60000, date(2026, 1, 1))  # 每期本金 10000

    service.advance_cutoff(date(2026, 2, 8))
    assert_points_match(service, inv, [(1, date(2026, 2, 8), 150, 150)])

    # 补录还款：与收费点同日，先计入再收费
    service.submit_repayment("R1", inv, 1, 4000, date(2026, 2, 8))
    assert_points_match(service, inv, [(1, date(2026, 2, 8), 90, 90)])

    # 推进截至日：同日多个收费点按期号升序
    service.advance_cutoff(date(2026, 3, 10))
    assert_points_match(service, inv, [
        (1, date(2026, 2, 8), 90, 90),
        (1, date(2026, 3, 10), 90, 90),
        (2, date(2026, 3, 10), 150, 150),
    ])

    # 更正金额与日期：2/8 当天不再计入，费用回升；3/10 剩余本金变为 5000
    before = snapshot(service)
    service.submit_repayment("R1", inv, 1, 5000, date(2026, 2, 9))
    assert_points_match(service, inv, [
        (1, date(2026, 2, 8), 150, 150),
        (1, date(2026, 3, 10), 75, 75),
        (2, date(2026, 3, 10), 150, 150),
    ])
    after = snapshot(service)
    assert after[: len(before)] == before  # 原账目未变
    assert len(after) > len(before)

    # 撤销还款（金额改 0）
    service.submit_repayment("R1", inv, 1, 0, date(2026, 2, 9))
    assert_points_match(service, inv, [
        (1, date(2026, 2, 8), 150, 150),
        (1, date(2026, 3, 10), 150, 150),
        (2, date(2026, 3, 10), 150, 150),
    ])

    # 相同请求重试不产生新账目
    before = snapshot(service)
    service.submit_repayment("R1", inv, 1, 0, date(2026, 2, 9))
    assert snapshot(service) == before

    # 收费点明细与账本一致性 + 借贷平衡
    assert_ledger_sound(service.ledger_entries())

    # 截至日只能向后推进
    with pytest.raises(ValueError):
        service.advance_cutoff(date(2026, 3, 9))

    # 归属不可更改
    with pytest.raises(ValueError):
        service.submit_repayment("R1", inv, 2, 100, date(2026, 2, 9))


def test_cap_release_and_restore(service):
    inv = "INV-2"
    principal = 10000  # 期本金 [1667,1667,1667,1667,1666,1666]，封顶 1500
    issue = date(2026, 1, 1)
    as_of = date(2027, 6, 1)
    service.register_invoice(inv, principal, issue)
    service.advance_cutoff(as_of)

    # 未还款：费用在靠前的收费点耗尽封顶额度，后续点为 0
    expected = reference_points(principal, issue, {}, as_of)
    assert_points_match(service, inv, expected)
    assert sum(p[3] for p in expected) == 1500
    assert any(p[3] == 0 and p[2] > 0 for p in expected)  # 存在被封顶阻挡的点

    # 补录第 1 期早期还款：早期点冲回，额度释放给后续点，总额仍封顶
    repaid = {1: [(date(2026, 2, 1), 1667)]}
    service.submit_repayment("R100", inv, 1, 1667, date(2026, 2, 1))
    expected2 = reference_points(principal, issue, repaid, as_of)
    assert_points_match(service, inv, expected2)
    assert sum(p[3] for p in expected2) == 1500  # 总额不变
    assert expected2 != expected                 # 但逐点净额不同
    first_inst1 = min(day for no, day, *_ in expected if no == 1)
    assert net_by_point(service.ledger_entries(inv))[(1, first_inst1)] == 0
    assert any(e.direction == "reversal" for e in service.ledger_entries(inv))

    # 撤销该还款：逐点结果恢复
    service.submit_repayment("R100", inv, 1, 0, date(2026, 2, 1))
    assert_points_match(service, inv, expected)
    assert_ledger_sound(service.ledger_entries())

    # 相同请求重试不增账
    before = snapshot(service)
    service.submit_repayment("R100", inv, 1, 0, date(2026, 2, 1))
    assert snapshot(service) == before
