from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from application.services.agents import AgentFactoryService
from application.services.contexts import ContextService
from application.services.conversations import ConversationService
from application.services.events import EventStreamService
from application.services.runs import RunOrchestrationService
from application.services.tools import ToolRegistryService
from infra.config import llm_client
from infra.db.mongodb import DocumentStore

from api.core.config import settings


class ServiceContainer:
    def __init__(self) -> None:
        self.root_dir = Path(__file__).resolve().parents[2]
        self.store = DocumentStore(settings.mongo_url, settings.mongo_db)
        self.events = EventStreamService(self.store)
        self.tools = ToolRegistryService(self.store, self.root_dir)
        self.contexts = ContextService(self.store)
        self.agents = AgentFactoryService(self.store, self.contexts, llm_client)
        self.runs = RunOrchestrationService(
            self.store,
            self.agents,
            self.contexts,
            self.events,
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


def get_conversation_service() -> ConversationService:
    return get_container().conversations

