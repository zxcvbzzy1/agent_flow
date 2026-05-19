from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from application.events.bridge import FrontendEventBridge
from application.events.human_confirmation import HumanConfirmationService
from application.services.agents import AgentFactoryService
from application.services.contexts import ContextService
from application.services.conversations import ConversationService
from application.services.events import EventStreamService
from application.services.runs import RunOrchestrationService
from application.services.tools import ToolRegistryService
from domain.runtime_hooks import (
    register_human_approval_provider,
    register_run_context_provider,
    register_tool_event_observer,
)
from infra.config import factory, llm_client
from infra.db.mongodb import DocumentStore

from api.core.config import settings


class ServiceContainer:
    def __init__(self) -> None:
        self.root_dir = Path(__file__).resolve().parents[2]
        self.store = DocumentStore(settings.mongo_url, settings.mongo_db)
        self.events = EventStreamService(self.store)
        self.frontend_bridge = FrontendEventBridge(self.events, factory)
        self.human_confirmations = HumanConfirmationService(self.events)
        register_tool_event_observer(self.frontend_bridge)
        register_human_approval_provider(self.human_confirmations)
        register_run_context_provider(self.frontend_bridge)
        self.tools = ToolRegistryService(self.store, self.root_dir)
        self.contexts = ContextService(self.store)
        self.agents = AgentFactoryService(self.store, self.contexts, llm_client, self.events)
        self.runs = RunOrchestrationService(
            self.store,
            self.agents,
            self.contexts,
            self.events,
            self.frontend_bridge,
        )
        self.conversations = ConversationService(self.store)


@lru_cache(maxsize=1)
def get_container() -> ServiceContainer:
    return ServiceContainer()


def get_store() -> DocumentStore:
    return get_container().store


def get_tool_service() -> ToolRegistryService:
    return get_container().tools


def get_context_service() -> ContextService:
    return get_container().contexts


def get_agent_service() -> AgentFactoryService:
    return get_container().agents


def get_run_service() -> RunOrchestrationService:
    return get_container().runs


def get_event_service() -> EventStreamService:
    return get_container().events


def get_human_confirmation_service() -> HumanConfirmationService:
    return get_container().human_confirmations


def get_conversation_service() -> ConversationService:
    return get_container().conversations
