from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from api.contexts.schemas import ContextCreateRequest
from api.core.dependencies import get_context_service
from application.services.contexts import ContextService


router = APIRouter(prefix="/api/contexts", tags=["contexts"])


@router.post("")
async def create_context(
    request: ContextCreateRequest,
    service: ContextService = Depends(get_context_service),
):
    item = service.create_context(
        kind=request.kind,
        name=request.name,
        provider_config=request.provider_config,
        strategy_config=request.strategy_config,
        available_fields=request.available_fields,
    )
    return {"item": item}


@router.get("/{context_id}")
async def get_context(
    context_id: str,
    service: ContextService = Depends(get_context_service),
):
    item = service.get_context(context_id)
    if item is None:
        raise HTTPException(status_code=404, detail="context not found")
    return {"item": item}

