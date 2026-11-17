# 定义契约 freightrate

## 数值

| 列 | 类型 | 量化 | 可空 | 说明 |
|---|---|---|---|---|
| weight_kg | Decimal | 0.001 kg | 否 | 实重，输入单位 g/kg/t 都换算到 kg |
| length_cm / width_cm / height_cm | Decimal | 0.001 cm | 否 | 尺寸，输入单位 cm/m/mm 都换算到 cm |
| volumetric_kg | Decimal | 0.001 kg | 否 | 长×宽×高 ÷ volume_divisor |
| billable_kg | Decimal | 0.5 kg 向上 | 否 | 取实重与体积重的较大值后按台阶进位 |
| first_price / step_price / min_charge | Decimal | 0.01 元 | 否 | 运价表金额 |
| fuel_pct / insurance_pct | Decimal | 小数 | 否 | 8.5% 与 0.085 两种写法都归到小数 |
| declared_value | Decimal | 0.01 元 | 是 | 空值与哨兵都按 0 处理，表示未保价 |
| total_amount | Decimal | 0.01 元 | 否 | 由分项求和得出，不单独舍入 |

金额与用量一律 Decimal，对外 JSON 用字符串。禁止 float 累加。

## 单位

单位表只在 `app/config.py` 定义一份。重量 g=0.001 kg、t=1000 kg；尺寸 m=100 cm、mm=0.1 cm。
带单位后缀的单元格（如 3200g）先剥后缀，单位列非空时以单位列为准。

## 时间

- 存储：UTC，ISO 文本，秒精度，带 Z 后缀
- 输入：接受 Z、+hh:mm、无偏移三种写法；无偏移按 +08:00 解释
- 展示：页面显示 Asia/Shanghai，生成时间与批次时间都带时区名
- 运价版本窗口：`effective_from` 与 `effective_to` 也是 UTC，区间半开 [from, to)

## 空值

- `null` / 空串 / 哨兵 三者语义分开
- 哨兵清单：`-`、`--`、`/`、`N/A`、`NA`、`NULL`、`NONE`、`未知`、`无`、`空`
- 重量与尺寸出现哨兵即进隔离区；声明价值出现哨兵按未保价处理

## 半开区间

运价版本用 `[effective_from, effective_to)`：提货时刻等于 from 命中该版，等于 to 落到下一版。
相邻版本首尾相接时不重不漏。

## 去重键

业务键是运单号 `waybill_no`。同一运单号第二次出现时比较载荷
（承运商、服务、分区、提货时刻、重量、三边尺寸、声明价值）：
载荷相同记 DUPLICATE_WAYBILL，不同记 DUP_CONFLICT，两种都只保留第一条参与计费。

## 空态

- 0 字节文件：拒绝采信，返回 EMPTY_FILE
- 只有表头：采信成功但 rows_in 为 0，页面显示空态
- 全是空行：empty_rows 计数，rows_in 为 0

## 原因码

原因码只在 `app/reasons.py` 定义，分两级：`block` 表示该行不进计费，`info` 表示只做标记。
每条隔离都带原始行号与原因码，可导出 CSV 复核。

## 服务变更

- 幂等键：优先取上游 `change_id`，缺失时用 `(运单号, 原服务, 实际服务, 来源, seq)` 的
  sha256 摘要兜底，落库为 `digest:<前16位>`；同一键重试返回原记录并标 CHANGE_DUPLICATE，不新增差异。
- 同票多条：按 `seq` 取最大者为当前有效变更，旧变更保留在台账但不参与重算。
- 两张表：
  - `service_changes` 变更台账：变更号、运单、原服务 `from_service`、实际服务 `to_service`、
    方向 `change_type`（downgrade/upgrade/same/changed）、来源、序号、状态、载荷摘要、接收时刻。
  - `cost_lines` 实际成本账：计费服务、实际服务、实际版本、实际计费重、五个成本分项、
    成本合计、客户应收 `receivable_amount`、降级损失 `downgrade_loss`、原因码。
- 口径：客户应收取 `charge_lines.total_amount` 冻结值，改服务不下调；
  降级损失 = 应收 − 成本，Decimal 量化到分，允许为负；无变更运单快照应收，损失为 0。
- 失效重算：成本账按当前变更台账整票全量替换，M5 重算或变更接收都会触发，不留陈旧行。
- 新增原因码：SERVICE_DOWNGRADED（档位降级，info）、SERVICE_CHANGED（其它方向变更，info）、
  CHANGE_DUPLICATE（重复送达，info）、CHANGE_WAYBILL_MISSING / CHANGE_CARRIER_MISMATCH（block，拒绝接收）、
  CHANGE_RATE_MISSING / COST_UNRATEABLE（实际服务无运价，成本不可核，全额应收挂损失）。
