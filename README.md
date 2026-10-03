# 分期融资逾期费核算库

Python + SQLite 实现，由上层直接调用，无接口层。金额一律为整数分。

## 能力

- `register_invoice(invoice_id, principal, issue_date)`：登记发票，固定 6 期，
  本金平均分配（余数从第 1 期起逐期多 1 分），第 i 期到期日为开票日后 30×i 天。
- `submit_repayment(repayment_id, invoice_id, installment_no, amount, occurred_date)`：
  登记或更正还款。同编号不同金额/日期表示更正，金额为 0 表示撤销，
  相同内容重复提交不产生新账目；归属（发票、期号）不可更改。
- `advance_cutoff(invoice_id, new_cutoff)`：推进计费截至日（只能向后）。
- `charge_points(invoice_id)`：查询收费点明细（剩余本金、原始费、实际费、净入账额）。
- `ledger(invoice_id)`：查询追加式账本（应收逾期费 / 费用收入，借贷平衡）。

## 规则

- 收费点：每期到期日后第 8 天起，每隔 30 天一个，直至截至日；
  同日还款先计入再收费，同日多收费点按期号升序。
- 原始费用 = 当日剩余本金 × 1.5%，四舍五入到分，不对既有费用计费。
- 单张发票累计费用封顶为原始本金 × 15%（四舍五入），按收费点顺序分配额度。
- 补录 / 更正 / 撤销还款后，调用返回前即修正截至日内全部受影响收费点；
  账本只追加，减少费用以反向分录冲回，旧账目保持不变。

## 用法

```python
from datetime import date
from overdue_fee import OverdueFeeService

with OverdueFeeService("fees.db") as svc:
    svc.register_invoice("INV1", 600000, date(2026, 1, 1))
    svc.advance_cutoff("INV1", date(2026, 3, 1))
    svc.submit_repayment("R1", "INV1", 1, 50000, date(2026, 2, 8))
    print(svc.charge_points("INV1"))
    print(svc.ledger("INV1"))
```

## 测试

```bash
python3 -m pytest tests/ -q
```
