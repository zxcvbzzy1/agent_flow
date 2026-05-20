from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from api.core.dependencies import get_tool_service
from api.tools.schemas import ToolUploadRequest
from application.services.tools import ToolRegistryService


router = APIRouter(prefix="/api/tools", tags=["tools"])


@router.get("")
async def list_tools(service: ToolRegistryService = Depends(get_tool_service)):
    return {"items": service.list_tools()}


@router.post("/upload")
async def upload_tool(
    request: ToolUploadRequest,
    service: ToolRegistryService = Depends(get_tool_service),
):
    tool = service.upload_tool(
        name=request.name,
        description=request.description,
        field=request.field,
        input_schema=request.input_schema,
        metadata=request.metadata,
        source_code=request.source_code,
    )
    return {"item": tool}


@router.delete("/{tool_id}")
async def delete_tool(
    tool_id: str,
    service: ToolRegistryService = Depends(get_tool_service),
):
    try:
        return {"item": service.delete_tool(tool_id)}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
