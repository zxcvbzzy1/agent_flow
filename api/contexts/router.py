from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from api.contexts.schemas import ContextCreateRequest
from api.core.dependencies import get_context_service
from application.services.contexts import ContextService


router = APIRouter(prefix="/api/contexts", tags=["contexts"])


@router.get("")
async def list_contexts(service: ContextService = Depends(get_context_service)):
    return {"items": service.list_contexts()}


@router.get("/catalog")
async def get_context_catalog(service: ContextService = Depends(get_context_service)):
    return {"item": service.catalog()}


@router.post("")
async def create_context(
    request: ContextCreateRequest,
    service: ContextService = Depends(get_context_service),
):
    try:
        item = service.create_context(
            kind=request.kind,
            name=request.name,
            provider_config=request.provider_config,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
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


@router.delete("/{context_id}")
async def delete_context(
    context_id: str,
    service: ContextService = Depends(get_context_service),
):
    try:
        return {"item": service.delete_context(context_id)}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
