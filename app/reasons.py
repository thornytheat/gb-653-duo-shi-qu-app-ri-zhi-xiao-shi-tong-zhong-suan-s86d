REASONS = {
    "BAD_COLUMNS": "字段数量不符", "EMPTY_VALUE": "必要值为空",
    "DUPLICATE_CONFLICT": "同一业务键取值冲突", "TRUNCATED": "尾记录不完整",
    "INVALID_VALUE": "值不符合定义", "STALE_VERSION": "提交版本已过期",
    "RECEIPT_UNKNOWN": "回执不存在", "RECEIPT_EXPIRED": "回执已过期",
    "CLAIM_RACED": "增量已被并发领取", "EMPTY": "无可领取增量",
    "NO_CHANGE": "桶值无变化", "MISMATCH": "累计结果与当前桶不一致",
}

def reason(code: str) -> str:
    return REASONS.get(code, "未登记原因")
