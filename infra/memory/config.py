from pathlib import Path

from application.services.long_memory import LongTermMemoryService
from infra.memory.files import MarkdownSourceFiles
from infra.memory.mongodb import MongoMemoryRepository


def build_long_memory(store, root: str | Path) -> LongTermMemoryService:
    return LongTermMemoryService(MongoMemoryRepository(store), MarkdownSourceFiles(root))
