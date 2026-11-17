from __future__ import annotations
import sqlite3
from datetime import timedelta
from pathlib import Path
from app.errors import DomainError
from . import core

SCHEMA = """
CREATE TABLE IF NOT EXISTS m7_bucket (
  hour TEXT NOT NULL, service TEXT NOT NULL,
  count INTEGER NOT NULL, errors INTEGER NOT NULL, updated_at TEXT NOT NULL,
  PRIMARY KEY (hour, service));
CREATE TABLE IF NOT EXISTS m7_baseline (
  hour TEXT NOT NULL, service TEXT NOT NULL,
  count INTEGER NOT NULL, errors INTEGER NOT NULL, seq INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (hour, service));
CREATE TABLE IF NOT EXISTS m7_delta (
  delta_id TEXT PRIMARY KEY,
  hour TEXT NOT NULL, service TEXT NOT NULL, seq INTEGER NOT NULL,
  count INTEGER NOT NULL, errors INTEGER NOT NULL, digest TEXT NOT NULL,
  status TEXT NOT NULL,
  receipt_id TEXT, claimed_by TEXT, claimed_at TEXT, expires_at TEXT, confirmed_at TEXT,
  created_at TEXT NOT NULL,
  UNIQUE (hour, service, seq));
CREATE TABLE IF NOT EXISTS m7_receipt (
  receipt_id TEXT PRIMARY KEY,
  consumer TEXT NOT NULL, status TEXT NOT NULL,
  issued_at TEXT NOT NULL, expires_at TEXT NOT NULL, confirmed_at TEXT);
"""

class DeliveryStore:
    """按(小时, 服务)保存当前桶、已确认基线与不可变增量，回执带租约。"""

    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        db = self.connect()
        try:
            db.executescript(SCHEMA)
        finally:
            db.close()

    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def _now(now) -> tuple:
        moment = core.parse_time(now) if now is not None else core.now_utc()
        return moment, core.fmt_time(moment)

    def revise(self, items: list, now=None) -> dict:
        """登记迟到修订：当前桶立即更新；与(基线+在途)的差额另记为新的补偿增量。"""
        moment, stamp = self._now(now)
        results = []
        db = self.connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            for raw in items:
                item = core.valid_revision(raw)
                key = (item["hour"], item["service"])
                db.execute("INSERT INTO m7_bucket VALUES (?,?,?,?,?) "
                           "ON CONFLICT(hour, service) DO UPDATE SET count=excluded.count, "
                           "errors=excluded.errors, updated_at=excluded.updated_at",
                           (*key, item["count"], item["errors"], stamp))
                base = db.execute("SELECT count, errors FROM m7_baseline WHERE hour=? AND service=?", key).fetchone()
                baseline = {"count": base["count"], "errors": base["errors"]} if base else {"count": 0, "errors": 0}
                agg = db.execute("SELECT COALESCE(SUM(CASE WHEN status!=? THEN count ELSE 0 END),0) AS c, "
                                 "COALESCE(SUM(CASE WHEN status!=? THEN errors ELSE 0 END),0) AS e, "
                                 "COALESCE(MAX(seq),0) AS s FROM m7_delta WHERE hour=? AND service=?",
                                 (core.CONFIRMED, core.CONFIRMED, *key)).fetchone()
                inc = core.increment(item, baseline, {"count": agg["c"], "errors": agg["e"]})
                if inc["count"] == 0 and inc["errors"] == 0:
                    results.append({**item, "delta_id": None, "increment": inc, "reason": "NO_CHANGE"})
                    continue
                seq = agg["s"] + 1
                did = core.delta_id(item["hour"], item["service"], seq, inc["count"], inc["errors"])
                db.execute("INSERT INTO m7_delta (delta_id, hour, service, seq, count, errors, digest, status, "
                           "receipt_id, claimed_by, claimed_at, expires_at, confirmed_at, created_at) "
                           "VALUES (?,?,?,?,?,?,?,?,NULL,NULL,NULL,NULL,NULL,?)",
                           (did, *key, seq, inc["count"], inc["errors"],
                            core.delta_digest(item["hour"], item["service"], seq, inc["count"], inc["errors"]),
                            core.PENDING, stamp))
                results.append({**item, "delta_id": did, "seq": seq, "increment": inc, "reason": "OK"})
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
        created = sum(1 for r in results if r["delta_id"])
        return {"status": "READY", "now": stamp, "input_count": len(results), "valid_count": len(results),
                "quarantine_count": 0, "created": created, "items": results, "reason": "OK"}

    def claim(self, consumer: str, now=None, lease_seconds: int = core.DEFAULT_LEASE_SECONDS,
              service: str | None = None, hour: str | None = None, limit: int = core.DEFAULT_LIMIT) -> dict:
        """原子领取待领取增量：签发带回执与租约，同一增量不会被并发重复占用。"""
        moment, stamp = self._now(now)
        try:
            lease_seconds = int(lease_seconds)
        except (TypeError, ValueError):
            raise DomainError("INVALID_VALUE", f"lease_seconds 不是整数: {lease_seconds!r}")
        if not 0 <= lease_seconds <= core.MAX_LEASE_SECONDS:
            raise DomainError("INVALID_VALUE", f"lease_seconds 超出范围: {lease_seconds}")
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            raise DomainError("INVALID_VALUE", f"limit 不是整数: {limit!r}")
        if not 1 <= limit <= core.MAX_LIMIT:
            raise DomainError("INVALID_VALUE", f"limit 超出范围: {limit}")
        consumer = str(consumer or "").strip() or "anonymous"
        expires = core.fmt_time(moment + timedelta(seconds=lease_seconds))
        where, params = "(status=? OR (status=? AND expires_at<=?))", [core.PENDING, core.CLAIMED, stamp]
        if service:
            where += " AND service=?"
            params.append(str(service))
        if hour:
            where += " AND hour=?"
            params.append(core.norm_hour(hour))
        db = self.connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(f"SELECT * FROM m7_delta WHERE {where} ORDER BY hour, service, seq LIMIT ?",
                              (*params, limit)).fetchall()
            if not rows:
                db.commit()
                return {"status": "READY", "receipt_id": None, "items": [], "reason": "EMPTY"}
            ids = [r["delta_id"] for r in rows]
            rid = core.receipt_id(consumer, ids, stamp, expires)
            db.execute("INSERT INTO m7_receipt VALUES (?,?,?,?,?,NULL)", (rid, consumer, "OPEN", stamp, expires))
            for row in rows:
                cur = db.execute("UPDATE m7_delta SET status=?, receipt_id=?, claimed_by=?, claimed_at=?, expires_at=? "
                                 "WHERE delta_id=? AND (status=? OR (status=? AND expires_at<=?))",
                                 (core.CLAIMED, rid, consumer, stamp, expires, row["delta_id"],
                                  core.PENDING, core.CLAIMED, stamp))
                if cur.rowcount != 1:
                    raise DomainError("CLAIM_RACED", f"增量 {row['delta_id']} 已被并发领取")
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
        items = [{"delta_id": r["delta_id"], "hour": r["hour"], "service": r["service"], "seq": r["seq"],
                  "count": r["count"], "errors": r["errors"], "digest": r["digest"]} for r in rows]
        return {"status": "READY", "receipt_id": rid, "consumer": consumer, "issued_at": stamp,
                "expires_at": expires, "items": items, "reason": "OK"}

    def confirm(self, receipt_id: str, now=None) -> dict:
        """凭回执确认：基线累加并落定增量；过期拒绝；重试重放原结果，不重复累计。"""
        moment, stamp = self._now(now)
        db = self.connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            receipt = db.execute("SELECT * FROM m7_receipt WHERE receipt_id=?", (receipt_id,)).fetchone()
            if receipt is None:
                raise DomainError("RECEIPT_UNKNOWN", f"回执不存在: {receipt_id}")
            if receipt["status"] == core.CONFIRMED:
                rows = db.execute("SELECT * FROM m7_delta WHERE receipt_id=? ORDER BY hour, service, seq",
                                  (receipt_id,)).fetchall()
                db.commit()
                items = [{"delta_id": r["delta_id"], "hour": r["hour"], "service": r["service"], "seq": r["seq"],
                          "count": r["count"], "errors": r["errors"], "digest": r["digest"]} for r in rows]
                return {"status": "READY", "receipt_id": receipt_id, "already_confirmed": True,
                        "confirmed_at": receipt["confirmed_at"], "items": items, "reason": "REPLAY"}
            if core.parse_time(receipt["expires_at"]) <= moment:
                raise DomainError("RECEIPT_EXPIRED", f"回执 {receipt_id} 已于 {receipt['expires_at']} 过期")
            rows = db.execute("SELECT * FROM m7_delta WHERE receipt_id=? AND status=? ORDER BY hour, service, seq",
                              (receipt_id, core.CLAIMED)).fetchall()
            sums: dict = {}
            for r in rows:
                key = (r["hour"], r["service"])
                acc = sums.setdefault(key, {"count": 0, "errors": 0, "n": 0})
                acc["count"] += r["count"]
                acc["errors"] += r["errors"]
                acc["n"] += 1
            for (hour, service), acc in sums.items():
                db.execute("INSERT INTO m7_baseline VALUES (?,?,0,0,0,?) ON CONFLICT(hour, service) DO NOTHING",
                           (hour, service, stamp))
                db.execute("UPDATE m7_baseline SET count=count+?, errors=errors+?, seq=seq+?, updated_at=? "
                           "WHERE hour=? AND service=?",
                           (acc["count"], acc["errors"], acc["n"], stamp, hour, service))
            db.execute("UPDATE m7_delta SET status=?, confirmed_at=? WHERE receipt_id=? AND status=?",
                       (core.CONFIRMED, stamp, receipt_id, core.CLAIMED))
            db.execute("UPDATE m7_receipt SET status=?, confirmed_at=? WHERE receipt_id=?",
                       (core.CONFIRMED, stamp, receipt_id))
            baselines = [{"hour": r["hour"], "service": r["service"], "count": r["count"],
                          "errors": r["errors"], "seq": r["seq"]}
                         for r in db.execute("SELECT * FROM m7_baseline ORDER BY hour, service").fetchall()]
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
        items = [{"delta_id": r["delta_id"], "hour": r["hour"], "service": r["service"], "seq": r["seq"],
                  "count": r["count"], "errors": r["errors"], "digest": r["digest"]} for r in rows]
        return {"status": "READY", "receipt_id": receipt_id, "already_confirmed": False,
                "confirmed_at": stamp, "items": items, "baselines": baselines, "reason": "OK"}

    def state(self, now=None, service: str | None = None, hour: str | None = None) -> dict:
        """回读每个(小时, 服务)的当前桶、基线与待领取/待确认/已确认增量。"""
        moment, stamp = self._now(now)
        where, params = "1=1", []
        if service:
            where += " AND service=?"
            params.append(str(service))
        if hour:
            where += " AND hour=?"
            params.append(core.norm_hour(hour))
        db = self.connect()
        try:
            buckets = {((r["hour"], r["service"])): r for r in
                       db.execute(f"SELECT * FROM m7_bucket WHERE {where}", params).fetchall()}
            baselines = {((r["hour"], r["service"])): r for r in
                         db.execute(f"SELECT * FROM m7_baseline WHERE {where}", params).fetchall()}
            deltas = db.execute(f"SELECT * FROM m7_delta WHERE {where} ORDER BY hour, service, seq", params).fetchall()
        finally:
            db.close()
        keys: dict = {}
        for row in deltas:
            keys.setdefault((row["hour"], row["service"]), {core.PENDING: [], core.CLAIMED: [], core.CONFIRMED: []})[
                core.classify(row, moment)].append(core.public_delta(row, moment))
        for key in set(buckets) | set(baselines):
            keys.setdefault(key, {core.PENDING: [], core.CLAIMED: [], core.CONFIRMED: []})
        out = []
        for (k_hour, k_service) in sorted(keys):
            classified = keys[(k_hour, k_service)]
            bucket = buckets.get((k_hour, k_service))
            baseline = baselines.get((k_hour, k_service))
            out.append({"hour": k_hour, "service": k_service,
                        "current": {"count": bucket["count"], "errors": bucket["errors"]} if bucket else None,
                        "baseline": {"count": baseline["count"], "errors": baseline["errors"], "seq": baseline["seq"]}
                        if baseline else {"count": 0, "errors": 0, "seq": 0},
                        "pending": classified[core.PENDING], "claimed": classified[core.CLAIMED],
                        "confirmed": classified[core.CONFIRMED],
                        "counts": {"pending": len(classified[core.PENDING]), "claimed": len(classified[core.CLAIMED]),
                                   "confirmed": len(classified[core.CONFIRMED])}})
        summary = {"pending": sum(k["counts"]["pending"] for k in out),
                   "claimed": sum(k["counts"]["claimed"] for k in out),
                   "confirmed": sum(k["counts"]["confirmed"] for k in out)}
        return {"status": "READY", "now": stamp, "keys": out, "summary": summary,
                "input_count": sum(summary.values()), "valid_count": summary["confirmed"],
                "quarantine_count": 0, "reason": "OK"}

    def reconcile(self, now=None) -> dict:
        """核对累计结果与当前桶：差额应等于在途增量，基线应等于已确认增量之和。"""
        moment, stamp = self._now(now)
        state = self.state(now=moment)
        rows = []
        for key in state["keys"]:
            if key["current"] is None:
                continue
            classified = {core.PENDING: key["pending"], core.CLAIMED: key["claimed"], core.CONFIRMED: key["confirmed"]}
            rows.append({"hour": key["hour"], "service": key["service"],
                         **core.reconcile_key(key["current"], key["baseline"], classified)})
        consistent = all(r["consistent"] and r["verified"] for r in rows)
        return {"status": "READY", "now": stamp, "consistent": consistent, "rows": rows,
                "reason": "OK" if consistent else "MISMATCH"}
