from __future__ import annotations
from datetime import datetime, timezone
from app.errors import DomainError
from app.manifest import canonical_digest

PENDING = "PENDING"      # 待领取
CLAIMED = "CLAIMED"      # 待确认
CONFIRMED = "CONFIRMED"  # 已确认

DEFAULT_LEASE_SECONDS = 60
MAX_LEASE_SECONDS = 86400
DEFAULT_LIMIT = 100
MAX_LIMIT = 500

SAMPLE = {"items": [
    {"hour": "2026-11-15T01:00:00Z", "service": "pay", "count": 3, "errors": 1},
    {"hour": "2026-11-15T02:00:00Z", "service": "pay", "count": 1, "errors": 0},
]}

def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def parse_time(value) -> datetime:
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)

def fmt_time(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

def norm_hour(value) -> str:
    return parse_time(value).strftime("%Y-%m-%dT%H:00:00Z")

def valid_revision(item: dict) -> dict:
    """校验并归一一条迟到修订：小时截断到整点，count/errors 必须是非负整数。"""
    try:
        hour = norm_hour(item["hour"])
    except Exception:
        raise DomainError("INVALID_VALUE", f"小时无法解析: {item.get('hour')!r}")
    service = str(item.get("service", "")).strip()
    if not service:
        raise DomainError("EMPTY_VALUE", "service 不能为空")
    out = {"hour": hour, "service": service}
    for field in ("count", "errors"):
        raw = item.get(field)
        if isinstance(raw, bool):
            raise DomainError("INVALID_VALUE", f"{field} 不是整数: {raw!r}")
        try:
            raw = int(str(raw))
        except (TypeError, ValueError):
            raise DomainError("INVALID_VALUE", f"{field} 不是整数: {item.get(field)!r}")
        if raw < 0:
            raise DomainError("INVALID_VALUE", f"{field} 不能为负: {raw}")
        out[field] = raw
    return out

def increment(current: dict, baseline: dict, in_flight: dict) -> dict:
    """当前桶值减去(已确认基线+在途增量)，得到需要补发的增量；可为负(向下补偿)。"""
    return {"count": current["count"] - (baseline["count"] + in_flight["count"]),
            "errors": current["errors"] - (baseline["errors"] + in_flight["errors"])}

def classify(delta, now: datetime) -> str:
    """按回执租约归类：已确认保持已确认；租约有效的领取为待确认；其余回到待领取。"""
    if delta["status"] == CONFIRMED:
        return CONFIRMED
    if delta["status"] == CLAIMED and delta["expires_at"] and parse_time(delta["expires_at"]) > now:
        return CLAIMED
    return PENDING

def delta_digest(hour: str, service: str, seq: int, count: int, errors: int) -> str:
    return canonical_digest({"hour": hour, "service": service, "seq": seq, "count": count, "errors": errors})

def delta_id(hour: str, service: str, seq: int, count: int, errors: int) -> str:
    return "dlv-" + delta_digest(hour, service, seq, count, errors)[:12]

def receipt_id(consumer: str, delta_ids: list, issued_at: str, expires_at: str) -> str:
    raw = {"consumer": consumer, "deltas": sorted(delta_ids), "issued_at": issued_at, "expires_at": expires_at}
    return "rcp-" + canonical_digest(raw)[:16]

def public_delta(row, now: datetime) -> dict:
    """增量对外视图：内容自创建起不变，状态按租约动态归类。"""
    return {"delta_id": row["delta_id"], "hour": row["hour"], "service": row["service"],
            "seq": row["seq"], "count": row["count"], "errors": row["errors"],
            "digest": row["digest"], "status": classify(row, now),
            "receipt_id": row["receipt_id"], "claimed_by": row["claimed_by"],
            "expires_at": row["expires_at"], "confirmed_at": row["confirmed_at"],
            "created_at": row["created_at"]}

def reconcile_key(current: dict, baseline: dict, classified: dict) -> dict:
    """核对单键：当前桶与已确认累计的差额，应恰好等于在途(待领取+待确认)增量。"""
    confirmed = {"count": sum(d["count"] for d in classified[CONFIRMED]),
                 "errors": sum(d["errors"] for d in classified[CONFIRMED])}
    outstanding = {"count": sum(d["count"] for d in classified[PENDING] + classified[CLAIMED]),
                   "errors": sum(d["errors"] for d in classified[PENDING] + classified[CLAIMED])}
    gap = {"count": current["count"] - baseline["count"], "errors": current["errors"] - baseline["errors"]}
    return {"current": current, "confirmed": baseline, "outstanding": outstanding, "gap": gap,
            "verified": baseline["count"] == confirmed["count"] and baseline["errors"] == confirmed["errors"],
            "consistent": gap == outstanding}
