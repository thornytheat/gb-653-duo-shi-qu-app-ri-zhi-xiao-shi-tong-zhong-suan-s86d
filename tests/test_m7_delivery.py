import pytest
from concurrent.futures import ThreadPoolExecutor
from app.errors import DomainError
from app.modules.m7_delivery import core
from app.modules.m7_delivery.store import DeliveryStore

T0 = "2026-11-16T10:00:00Z"
T1 = "2026-11-16T10:00:30Z"
T2 = "2026-11-16T10:01:31Z"  # T0 + 91s, 越过 60s 与 90s 租约
KEY = {"hour": "2026-11-15T01:00:00Z", "service": "pay"}

@pytest.fixture
def store(tmp_path):
    return DeliveryStore(str(tmp_path / "delivery.db"))

def revise(store, count, errors=0, now=T0, **key):
    item = {**KEY, **key, "count": count, "errors": errors}
    return store.revise([item], now=now)

def test_revise_updates_bucket_immediately(store):
    out = revise(store, 3, 1)
    assert out["created"] == 1 and out["items"][0]["increment"] == {"count": 3, "errors": 1}
    state = store.state(now=T1)
    assert state["keys"][0]["current"] == {"count": 3, "errors": 1}
    assert state["keys"][0]["counts"]["pending"] == 1

def test_revise_retry_no_duplicate_delta(store):
    revise(store, 3, 1)
    again = revise(store, 3, 1, now=T1)
    assert again["items"][0]["reason"] == "NO_CHANGE" and again["created"] == 0
    assert store.state(now=T1)["summary"]["pending"] == 1

def test_revise_normalizes_hour_and_validates(store):
    out = store.revise([{"hour": "2026-11-15T01:59:59Z", "service": "pay", "count": 1, "errors": 0}], now=T0)
    assert out["items"][0]["hour"] == "2026-11-15T01:00:00Z"
    with pytest.raises(DomainError):
        store.revise([{"hour": "2026-11-15T01:00:00Z", "service": "pay", "count": -1, "errors": 0}], now=T0)

def test_claim_blocks_second_claim_until_expiry(store):
    revise(store, 3, 1)
    first = store.claim("worker-a", now=T0, lease_seconds=60)
    assert first["receipt_id"] and len(first["items"]) == 1
    second = store.claim("worker-b", now=T1, lease_seconds=60)
    assert second["receipt_id"] is None and second["reason"] == "EMPTY"

def test_claimed_delta_keeps_original_and_revision_becomes_compensation(store):
    revise(store, 3, 1)
    claim = store.claim("worker-a", now=T0, lease_seconds=90)
    original = claim["items"][0]
    revise(store, 5, 2, now=T1)  # 已领取未确认期间又有迟到修订
    state = store.state(now=T1)
    claimed = state["keys"][0]["claimed"][0]
    assert claimed["delta_id"] == original["delta_id"]
    assert (claimed["count"], claimed["errors"]) == (3, 1)  # 原内容不被覆盖
    pending = state["keys"][0]["pending"]
    assert len(pending) == 1 and (pending[0]["count"], pending[0]["errors"]) == (2, 1)  # 另记补偿
    store.confirm(claim["receipt_id"], now=T1)
    follow = store.claim("worker-a", now=T1, lease_seconds=60)
    store.confirm(follow["receipt_id"], now=T1)
    assert store.reconcile(now=T1)["rows"][0]["confirmed"] == {"count": 5, "errors": 2, "seq": 2}

def test_confirm_advances_baseline_and_retry_replays(store):
    revise(store, 3, 1)
    claim = store.claim("worker-a", now=T0, lease_seconds=60)
    done = store.confirm(claim["receipt_id"], now=T1)
    assert done["already_confirmed"] is False
    assert done["baselines"][0]["count"] == 3
    replay = store.confirm(claim["receipt_id"], now=T1)
    assert replay["already_confirmed"] is True and replay["reason"] == "REPLAY"
    assert replay["items"] == done["items"]
    state = store.state(now=T1)  # 重试不重复累计
    assert state["keys"][0]["baseline"]["count"] == 3
    assert state["keys"][0]["counts"]["confirmed"] == 1

def test_expired_receipt_rejected(store):
    revise(store, 3, 1)
    claim = store.claim("worker-a", now=T0, lease_seconds=60)
    with pytest.raises(DomainError) as err:
        store.confirm(claim["receipt_id"], now=T2)
    assert err.value.code == "RECEIPT_EXPIRED"
    assert store.state(now=T2)["keys"][0]["baseline"]["count"] == 0

def test_unknown_receipt_rejected(store):
    with pytest.raises(DomainError) as err:
        store.confirm("rcp-missing", now=T0)
    assert err.value.code == "RECEIPT_UNKNOWN"

def test_reclaim_after_expiry_returns_original_content(store):
    revise(store, 3, 1)
    first = store.claim("worker-a", now=T0, lease_seconds=60)
    second = store.claim("worker-b", now=T2, lease_seconds=60)  # 租约过期后重领
    assert second["receipt_id"] != first["receipt_id"]
    assert second["items"] == first["items"]  # 原件内容不变
    with pytest.raises(DomainError):  # 旧回执已过期
        store.confirm(first["receipt_id"], now=T2)
    done = store.confirm(second["receipt_id"], now=T2)
    assert done["baselines"][0]["count"] == 3

def test_restart_recovery(tmp_path):
    path = str(tmp_path / "delivery.db")
    store = DeliveryStore(path)
    revise(store, 3, 1)
    claim = store.claim("worker-a", now=T0, lease_seconds=600)
    reopened = DeliveryStore(path)  # 模拟服务重启
    done = reopened.confirm(claim["receipt_id"], now=T1)
    assert done["already_confirmed"] is False
    assert reopened.state(now=T1)["keys"][0]["baseline"]["count"] == 3

def test_concurrent_claims_no_double_occupancy(tmp_path):
    store = DeliveryStore(str(tmp_path / "delivery.db"))
    store.revise([{**KEY, "count": 8, "errors": 0},
                  {"hour": KEY["hour"], "service": "search", "count": 5, "errors": 0},
                  {"hour": "2026-11-15T02:00:00Z", "service": "pay", "count": 2, "errors": 0}], now=T0)
    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = list(pool.map(lambda _: store.claim("worker", lease_seconds=60), range(8)))
    delivered = [item["delta_id"] for claim in claims for item in claim["items"]]
    assert len(delivered) == 3 and len(set(delivered)) == 3  # 无重复占用
    assert sum(1 for claim in claims if claim["receipt_id"]) >= 1

def test_state_readback_groups_three_states(store):
    revise(store, 3, 1)
    store.revise([{**KEY, "service": "search", "count": 2, "errors": 0}], now=T0)
    claim = store.claim("worker-a", now=T0, lease_seconds=90, service="pay")
    store.confirm(claim["receipt_id"], now=T1)
    revise(store, 4, 1, now=T1)
    state = store.state(now=T1)
    by_service = {k["service"]: k for k in state["keys"]}
    assert by_service["pay"]["counts"] == {"pending": 1, "claimed": 0, "confirmed": 1}
    assert by_service["search"]["counts"] == {"pending": 1, "claimed": 0, "confirmed": 0}
    assert state["summary"] == {"pending": 2, "claimed": 0, "confirmed": 1}

def test_reconcile_gap_equals_outstanding(store):
    revise(store, 3, 1)
    claim = store.claim("worker-a", now=T0, lease_seconds=90)
    revise(store, 5, 2, now=T1)
    mid = store.reconcile(now=T1)
    row = mid["rows"][0]
    assert mid["consistent"] and row["verified"]
    assert row["gap"] == {"count": 5, "errors": 2}  # 当前桶与已确认累计的差异
    assert row["outstanding"] == row["gap"]  # 差异恰好是在途增量
    store.confirm(claim["receipt_id"], now=T1)
    follow = store.claim("worker-a", now=T1, lease_seconds=60)
    store.confirm(follow["receipt_id"], now=T1)
    final = store.reconcile(now=T1)
    assert final["consistent"] and final["rows"][0]["gap"] == {"count": 0, "errors": 0}

def test_downward_revision_records_negative_compensation(store):
    revise(store, 5, 1)
    claim = store.claim("worker-a", now=T0, lease_seconds=90)
    store.confirm(claim["receipt_id"], now=T1)
    out = revise(store, 4, 1, now=T1)
    assert out["items"][0]["increment"] == {"count": -1, "errors": 0}
    back = store.claim("worker-a", now=T1, lease_seconds=60)
    store.confirm(back["receipt_id"], now=T1)
    row = store.reconcile(now=T1)["rows"][0]
    assert row["confirmed"]["count"] == 4 and row["gap"] == {"count": 0, "errors": 0}

def test_core_classify_boundary():
    delta = {"status": core.CLAIMED, "expires_at": "2026-11-16T10:01:00Z"}
    assert core.classify(delta, core.parse_time("2026-11-16T10:00:59Z")) == core.CLAIMED
    assert core.classify(delta, core.parse_time("2026-11-16T10:01:00Z")) == core.PENDING
    assert core.classify({"status": core.CONFIRMED, "expires_at": None}, core.parse_time(T2)) == core.CONFIRMED

def test_api_full_cycle(client):
    res = client.post("/api/delivery/revise", json={"items": [{**KEY, "count": 3, "errors": 1}]})
    assert res.status_code == 200 and res.json()["created"] == 1
    claim = client.post("/api/delivery/claim", json={"consumer": "worker-a"}).json()
    assert claim["receipt_id"] and claim["items"][0]["count"] == 3
    done = client.post("/api/delivery/confirm", json={"receipt_id": claim["receipt_id"]})
    assert done.status_code == 200 and done.json()["already_confirmed"] is False
    replay = client.post("/api/delivery/confirm", json={"receipt_id": claim["receipt_id"]}).json()
    assert replay["already_confirmed"] is True
    state = client.get("/api/delivery/state").json()
    assert state["summary"] == {"pending": 0, "claimed": 0, "confirmed": 1}
    recon = client.get("/api/delivery/reconcile").json()
    assert recon["consistent"] and recon["rows"][0]["gap"] == {"count": 0, "errors": 0}

def test_api_expired_and_unknown_receipt(client):
    client.post("/api/delivery/revise", json={"items": [{**KEY, "count": 3, "errors": 0}]})
    claim = client.post("/api/delivery/claim", json={"lease_seconds": 0}).json()
    res = client.post("/api/delivery/confirm", json={"receipt_id": claim["receipt_id"]})
    assert res.status_code == 422 and res.json()["error"] == "RECEIPT_EXPIRED"
    missing = client.post("/api/delivery/confirm", json={"receipt_id": "rcp-missing"})
    assert missing.status_code == 422 and missing.json()["error"] == "RECEIPT_UNKNOWN"

def test_api_page(client):
    res = client.get("/delivery")
    assert res.status_code == 200 and "panel" in res.text
