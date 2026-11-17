# 数据字典

| 字段 | 类型 | 可空 | 定义 |
|---|---|---:|---|
| `run_id` | string | 否 | 输入摘要派生的稳定运行编号 |
| `source_line` | integer | 否 | 原文件物理行号 |
| `reason_code` | string | 是 | 隔离或冲突的稳定原因码 |
| `raw_value` | string | 是 | 未归一的原始证据 |
| `normalized_value` | string | 是 | 按项目规则得到的值 |
| `rule_version` | string | 否 | 计算规则版本 |
| `delta_id` | string | 否 | 增量编号，由(小时, 服务, 序号, 内容)摘要派生 |
| `receipt_id` | string | 否 | 领取回执编号，确认与重放的凭据 |
| `seq` | integer | 否 | 同一(小时, 服务)下增量的单调序号 |
| `expires_at` | string | 是 | 回执租约截止时刻，过期拒绝确认并可重领 |
| `baseline` | object | 否 | 已确认累计(count/errors/seq)，按(小时, 服务)保存 |

字符串编号逐字保留。金额与计量值用 `Decimal`。时间值带偏移并以 UTC 参与比较。区间统一采用左闭右开。
