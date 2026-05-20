from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from api.core.dependencies import (
    get_event_service,
    get_human_confirmation_service,
    get_run_service,
)
from api.runs.schemas import ConfirmationResolveRequest, RunCreateRequest
from application.events.human_confirmation import HumanConfirmationService
from application.services.events import EventStreamService
from application.services.runs import RunOrchestrationService


router = APIRouter(prefix="/api/runs", tags=["runs"])


@router.post("")
async def create_run(
    request: RunCreateRequest,
    service: RunOrchestrationService = Depends(get_run_service),
):
    item = service.create_run(
        prompt=request.prompt,
        planner_agent_id=request.planner_agent_id,
        executor_agent_ids=request.executor_agent_ids,
        context_id=request.context_id,
        max_replan_rounds=request.max_replan_rounds,
        conversation_id=request.conversation_id,
        message_id=request.message_id,
        auto_start=request.auto_start,
    )
    return {"item": item}


@router.get("/{run_id}")
async def get_run(
    run_id: str,
    service: RunOrchestrationService = Depends(get_run_service),
):
    item = service.get_run(run_id)
    if item is None:
        raise HTTPException(status_code=404, detail="run not found")
    return {"item": item}


@router.post("/{run_id}/cancel")
async def cancel_run(
    run_id: str,
    service: RunOrchestrationService = Depends(get_run_service),
):
    try:
        item = service.cancel_run(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"item": item}


@router.get("/{run_id}/events")
async def stream_run_events(
    run_id: str,
    service: EventStreamService = Depends(get_event_service),
):
    return StreamingResponse(
        service.stream(run_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


@router.get("/{run_id}/confirmations")
async def list_run_confirmations(
    run_id: str,
    service: HumanConfirmationService = Depends(get_human_confirmation_service),
):
    return {"items": service.list_pending(run_id)}


@router.post("/{run_id}/confirmations/{confirmation_id}")
async def resolve_run_confirmation(
    run_id: str,
    confirmation_id: str,
    request: ConfirmationResolveRequest,
    service: HumanConfirmationService = Depends(get_human_confirmation_service),
):
    try:
        item = service.resolve(
            run_id=run_id,
            confirmation_id=confirmation_id,
            approved=request.approved,
            reason=request.reason,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"item": item}
