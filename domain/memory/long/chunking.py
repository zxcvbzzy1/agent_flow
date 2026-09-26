from __future__ import annotations

import math
import re

from .ports import TokenCounter


class EstimatedTokenCounter:
    name = "cjk-char-latin-4-v1"

    def count(self, text: str) -> int:
        cjk = len(re.findall(r"[\u3400-\u9fff]", text))
        return cjk + math.ceil((len(text) - cjk) / 4)


class LineChunker:
    """Split on paragraph/line boundaries, never altering the original lines."""

    def __init__(self, counter: TokenCounter, target_tokens: int = 1000):
        self.counter = counter
        self.target_tokens = target_tokens

    def split(self, text: str, section: dict) -> list[dict]:
        lines = text.splitlines()
        start, stop = section["start_line"] - 1, section["end_line"]
        chunks = []
        while start < stop:
            end, paragraph_end = start, None
            while end < stop:
                if end > start and self.counter.count("\n".join(lines[start:end + 1])) > self.target_tokens:
                    break
                end += 1
                if not lines[end - 1].strip():
                    paragraph_end = end
            if end < stop and paragraph_end is not None:
                end = paragraph_end
            content = "\n".join(lines[start:end])
            if content.strip():
                chunks.append({**section, "start_line": start + 1, "end_line": end, "content": content})
            start = end
        return chunks
