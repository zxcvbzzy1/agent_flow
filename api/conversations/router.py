from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from api.conversations.schemas import (
    ConversationCreateRequest,
    ConversationRunCreateRequest,
    MessageCreateRequest,
    QueueCreateRequest,
)
from api.core.dependencies import get_conversation_service, get_run_service
from application.services.conversations import ConversationService
from application.services.runs import RunOrchestrationService


router = APIRouter(prefix="/api/conversations", tags=["conversations"])


@router.post("")
async def create_conversation(
    request: ConversationCreateRequest,
    service: ConversationService = Depends(get_conversation_service),
):
    return {"item": service.create_conversation(request.title, request.metadata)}


@router.get("")
async def list_conversations(service: ConversationService = Depends(get_conversation_service)):
    return {"items": service.list_conversations()}


@router.get("/{conversation_id}")
async def get_conversation(
    conversation_id: str,
    service: ConversationService = Depends(get_conversation_service),
):
    item = service.get_conversation(conversation_id)
    if item is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    return {"item": item}


@router.delete("/{conversation_id}")
async def delete_conversation(
    conversation_id: str,
    service: ConversationService = Depends(get_conversation_service),
):
    try:
        return {"item": service.delete_conversation(conversation_id)}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/{conversation_id}/messages")
async def list_messages(
    conversation_id: str,
    service: ConversationService = Depends(get_conversation_service),
):
    return {"items": service.list_messages(conversation_id)}


@router.post("/{conversation_id}/messages")
async def add_message(
    conversation_id: str,
    request: MessageCreateRequest,
    service: ConversationService = Depends(get_conversation_service),
):
    try:
        item = service.add_message(
            conversation_id=conversation_id,
            role=request.role,
            content=request.content,
            metadata=request.metadata,
            run_id=request.run_id,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"item": item}


@router.post("/{conversation_id}/queue")
async def enqueue_message(
    conversation_id: str,
    request: QueueCreateRequest,
    service: ConversationService = Depends(get_conversation_service),
):
    try:
        item = service.enqueue_message(
            conversation_id=conversation_id,
            message_id=request.message_id,
            metadata=request.metadata,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"item": item}


@router.get("/{conversation_id}/queue")
async def list_queue(
    conversation_id: str,
    service: ConversationService = Depends(get_conversation_service),
):
    return {"items": service.list_queue(conversation_id)}


@router.post("/{conversation_id}/runs")
async def create_run_from_conversation(
    conversation_id: str,
    request: ConversationRunCreateRequest,
    conversations: ConversationService = Depends(get_conversation_service),
    runs: RunOrchestrationService = Depends(get_run_service),
):
    try:
        message = conversations.latest_pending_user_message(conversation_id)
        run = runs.create_run(
            prompt=message["content"],
            planner_agent_id=request.planner_agent_id,
            executor_agent_ids=request.executor_agent_ids,
            context_id=request.context_id,
            max_replan_rounds=request.max_replan_rounds,
            conversation_id=conversation_id,
            message_id=message["message_id"],
        )
        conversations.mark_queue_processing(
            conversation_id=conversation_id,
            message_id=message["message_id"],
            run_id=run["run_id"],
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"item": run}
