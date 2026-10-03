# task26100551

分期融资逾期费核算库。上层直接调用 `OverdueFeeService`，数据持久化在 SQLite，无接口层。

## 规则

- 发票固定 6 期，金额为整数分；本金平均分配，余数从第 1 期起逐期多 1 分；第 i 期到期日为开票日后 30*i 个自然日。
- 收费点：到期日后第 8 天起，每隔 30 天一个，直到计费截至日。同日还款先计入再收费；同日多个收费点按期号升序。
- 每点原始费用 = 当日剩余本金 × 1.5%（四舍五入到分），不对既有费用计费。
- 每张发票累计费用 ≤ 原始本金 × 15%（四舍五入到分），按收费点顺序分配额度，额度耗尽后为 0。
- 账本只追加：增加费用记「应收逾期费」借方 /「费用收入」贷方，减少记反向冲回；旧账目不变。

## 用法

```python
from datetime import date
from overdue_fees import OverdueFeeService

svc = OverdueFeeService("fees.db")
svc.register_invoice("INV-1", 60000, date(2026, 1, 1))   # 本金 600.00 元
svc.advance_cutoff(date(2026, 3, 10))                    # 推进计费截至日（只能向后）
svc.submit_repayment("R1", "INV-1", 1, 4000, date(2026, 2, 8))  # 登记还款
svc.submit_repayment("R1", "INV-1", 1, 5000, date(2026, 2, 9))  # 同编号更正
svc.submit_repayment("R1", "INV-1", 1, 0, date(2026, 2, 9))     # 金额 0 = 撤销

for p in svc.charge_points("INV-1"):   # 收费点明细
    print(p)
for e in svc.ledger_entries("INV-1"):  # 费用账本
    print(e)
svc.close()
```

补录、更正、撤销还款后，截至日内所有受影响收费点（含原本费用为 0 的点）在调用返回前完成修正；相同请求重试不产生新账目。

## 测试

```bash
python3 -m pytest tests -q
```
