from __future__ import annotations

from fastapi import APIRouter, Depends

from api.agents.schemas import AgentCreateRequest
from api.core.dependencies import get_agent_service
from application.services.agents import AgentFactoryService


router = APIRouter(prefix="/api/agents", tags=["agents"])


@router.get("")
async def list_agents(service: AgentFactoryService = Depends(get_agent_service)):
    return {"items": service.list_agents()}


@router.post("")
async def create_agent(
    request: AgentCreateRequest,
    service: AgentFactoryService = Depends(get_agent_service),
):
    item = service.create_agent(
        name=request.name,
        agent_type=request.agent_type,
        context_id=request.context_id,
        role_prompt=request.role_prompt,
        metadata=request.metadata,
    )
    return {"item": item}

