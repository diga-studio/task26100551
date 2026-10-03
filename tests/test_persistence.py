"""跨进程重开后数据持久化的验证。"""

from datetime import date

from overdue_fees import OverdueFeeService


def test_persistence_across_reopen(tmp_path):
    db = tmp_path / "fees.db"
    svc = OverdueFeeService(db)
    svc.register_invoice("A", 60000, date(2026, 1, 1))
    svc.advance_cutoff(date(2026, 3, 10))
    svc.submit_repayment("R1", "A", 1, 4000, date(2026, 2, 8))
    svc.close()

    svc2 = OverdueFeeService(db)
    points = svc2.charge_points("A")
    assert [(p.installment_no, p.charge_date, p.net_fee) for p in points] == [
        (1, date(2026, 2, 8), 90),
        (1, date(2026, 3, 10), 90),
        (2, date(2026, 3, 10), 150),
    ]
    entries = svc2.ledger_entries("A")
    assert len(entries) == 5  # 3 笔记账 + 2 笔冲回
    assert sum(
        e.amount if e.direction == "charge" else -e.amount for e in entries
    ) == 330
    svc2.close()
