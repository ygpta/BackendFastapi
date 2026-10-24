from fastapi import APIRouter, Depends

from backendfastapi.api.v1.endpoints import (
    auth,
    chat,
    dashboard,
    platform,
    runs,
    tickets,
    webhooks,
)
from backendfastapi.core.identity import get_principal

router = APIRouter()
router.include_router(auth.router)
protected = APIRouter(dependencies=[Depends(get_principal)])
protected.include_router(tickets.router)
protected.include_router(chat.router)
protected.include_router(dashboard.router)
protected.include_router(runs.router)
protected.include_router(webhooks.router)
protected.include_router(platform.router)
router.include_router(protected)
