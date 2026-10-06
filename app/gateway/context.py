"""Context windows (spec 5.5).

Each model's window comes from LiteLLM's model map, with a configuration
override. The prompt is built once and reused on fallback, so the check uses the
smallest window among the planned models. A model that neither source knows gets
a deliberately small default and is reported as such.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from app.gateway.errors import ContextWindowExceeded

DEFAULT_UNKNOWN_WINDOW = 32_000


class ContextWindows:
    def __init__(self, overrides: Mapping[str, int] | None = None) -> None:
        self._overrides = dict(overrides or {})

    def window(self, model: str) -> tuple[int, str]:
        """Return (tokens, source) where source is 'override', 'litellm' or 'default'."""
        if model in self._overrides:
            return self._overrides[model], "override"
        import litellm

        info = litellm.model_cost.get(model) or {}
        tokens = info.get("max_input_tokens")
        if isinstance(tokens, int) and tokens > 0:
            return tokens, "litellm"
        return DEFAULT_UNKNOWN_WINDOW, "default"

    def smallest(self, models: Iterable[str]) -> int:
        windows = [self.window(model)[0] for model in models]
        return min(windows) if windows else DEFAULT_UNKNOWN_WINDOW

    def check(self, prompt_tokens: int, reserved_output: int, models: Iterable[str]) -> int:
        """Raise ContextWindowExceeded if the prompt plus reserved output does not fit."""
        smallest = self.smallest(models)
        if prompt_tokens + reserved_output > smallest:
            raise ContextWindowExceeded()
        return smallest
