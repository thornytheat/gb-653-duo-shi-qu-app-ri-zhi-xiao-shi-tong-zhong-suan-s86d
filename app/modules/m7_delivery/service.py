from __future__ import annotations
from app.errors import DomainError
from . import core
from .store import DeliveryStore

def _items(payload: dict) -> list:
    if payload.get("items"):
        return payload["items"]
    if payload.get("hour") is not None or payload.get("service") is not None:
        return [payload]
    return core.SAMPLE["items"]

def revise(store: DeliveryStore, payload: dict | None = None, now=None) -> dict:
    return store.revise(_items(payload or {}), now=now)

def claim(store: DeliveryStore, payload: dict | None = None, now=None) -> dict:
    payload = payload or {}
    return store.claim(consumer=payload.get("consumer", "anonymous"),
                       lease_seconds=payload.get("lease_seconds", core.DEFAULT_LEASE_SECONDS),
                       service=payload.get("service"), hour=payload.get("hour"),
                       limit=payload.get("limit", core.DEFAULT_LIMIT), now=now)

def confirm(store: DeliveryStore, payload: dict | None = None, now=None) -> dict:
    receipt_id = (payload or {}).get("receipt_id")
    if not receipt_id:
        raise DomainError("RECEIPT_UNKNOWN", "缺少 receipt_id")
    return store.confirm(str(receipt_id), now=now)

def state(store: DeliveryStore, service: str | None = None, hour: str | None = None, now=None) -> dict:
    return store.state(now=now, service=service, hour=hour)

def reconcile(store: DeliveryStore, now=None) -> dict:
    return store.reconcile(now=now)
