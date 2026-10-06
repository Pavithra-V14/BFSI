"""Token counting with a real tokenizer (spec 5.4): tiktoken, o200k_base.

o200k_base is the encoding of the gpt-oss family. For Gemini and Gemma models the
count is an approximation, so provider-reported usage is preferred whenever the
provider returns it. If the encoding file cannot be loaded (it is downloaded on
first use), counting falls back to a rough length estimate and says so.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

PER_MESSAGE_OVERHEAD = 4


class TokenCounter:
    def __init__(self, encoding_name: str = "o200k_base") -> None:
        self._name = encoding_name
        self._encoding: Any = None
        self._loaded = False
        self.approximate = False

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            import tiktoken

            self._encoding = tiktoken.get_encoding(self._name)
        except Exception:  # offline, or the cache is missing: estimate instead
            self._encoding = None
            self.approximate = True

    def count_text(self, text: str) -> int:
        self._load()
        if self._encoding is None:
            return math.ceil(len(text) / 4)
        return len(self._encoding.encode(text, disallowed_special=()))

    def count_messages(self, messages: Sequence[dict[str, Any]]) -> int:
        total = 2
        for message in messages:
            total += PER_MESSAGE_OVERHEAD + self.count_text(str(message.get("content", "")))
        return total
