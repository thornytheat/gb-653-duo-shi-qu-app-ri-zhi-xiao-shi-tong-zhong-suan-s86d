from fastapi import APIRouter, Body, Request
from . import service as svc

router = APIRouter(tags=["M7-修订交付"])

@router.get("/delivery")
def page(request: Request):
    data = svc.state(request.app.state.delivery)
    return request.app.state.templates.TemplateResponse(request, "m7_delivery.html", {"title": "修订交付", "data": data})

@router.post("/api/delivery/revise")
def revise(request: Request, payload: dict = Body(default={})):
    return svc.revise(request.app.state.delivery, payload or {})

@router.post("/api/delivery/claim")
def claim(request: Request, payload: dict = Body(default={})):
    return svc.claim(request.app.state.delivery, payload or {})

@router.post("/api/delivery/confirm")
def confirm(request: Request, payload: dict = Body(default={})):
    return svc.confirm(request.app.state.delivery, payload or {})

@router.get("/api/delivery/state")
def state(request: Request, service: str | None = None, hour: str | None = None):
    return svc.state(request.app.state.delivery, service=service, hour=hour)

@router.get("/api/delivery/reconcile")
def reconcile(request: Request):
    return svc.reconcile(request.app.state.delivery)
