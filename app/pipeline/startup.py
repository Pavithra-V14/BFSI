"""The masker loads in the background; until it is ready the answer is "starting up".

The first masking call loads the spaCy model (about 40 seconds on a small machine, spec/README
Phase 6). The API must not wait for that at start-up, and it must NEVER skip masking meanwhile:
`MaskerHolder.start()` builds the masker in a thread, and until it is ready the pipeline answers
"starting up, please retry" (the API maps it to a 503). If the load fails the holder says so (an
alert event) and the pipeline answers "temporarily unavailable"; it never answers unmasked.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from app.guardrails.masking import Masker


class MaskerHolder:
    def __init__(
        self,
        factory: Callable[[], Masker] = Masker,
        *,
        warm_up: str = "Warm up the name model.",
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self._factory = factory
        self._warm_up = warm_up
        self._on_event = on_event
        self._ready = threading.Event()
        self._masker: Masker | None = None
        self.failed = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._load, name="masker-load", daemon=True)
        self._thread.start()

    def _load(self) -> None:
        try:
            masker = self._factory()
            masker.mask(self._warm_up)  # loads the model now, not on the first user's question
            self._masker = masker
            self._ready.set()
        except Exception as error:
            self.failed = True
            if self._on_event:
                self._on_event("masker_load_failed", {"reason": type(error).__name__})

    @property
    def ready(self) -> bool:
        return self._ready.is_set()

    def wait(self, timeout: float | None = None) -> bool:
        return self._ready.wait(timeout)

    @property
    def masker(self) -> Masker:
        if self._masker is None:
            raise RuntimeError("the masker is not ready")
        return self._masker

    @classmethod
    def ready_with(cls, masker: Masker) -> MaskerHolder:
        """A holder that is already ready (tests, and callers that built the masker themselves)."""
        holder = cls(lambda: masker)
        holder._masker = masker
        holder._ready.set()
        return holder
