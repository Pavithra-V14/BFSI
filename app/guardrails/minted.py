"""Text types that only one function may create (spec 9.2 "masked text only", CLAUDE.md invariant).

`StoredQuery` and `StoredAnswer` (made by `mask_for_storage`) and `StoredSummary` (made by
`fold_summary`) mean "this text has been masked the way the rule requires". A type check alone is
worth little if anyone can write `StoredQuery("a raw phone number")`, so these types cannot be built
by calling the class: `__init__` always refuses, `dataclasses.replace` and a subclass go through
`__init__` and are refused too, and copying or pickling is refused. The one door is `mint`, imported
only by the function that makes the type (a test lists every module that mentions it) and by the
test-only builder in `tests/stored.py`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar, TypeVar

_MINT = object()
T = TypeVar("T", bound="MintedText")


@dataclass(frozen=True)
class MintedText:
    text: str
    _token: object = field(default=None, init=False, repr=False, compare=False)

    refusal: ClassVar[str] = "this type can only be made by its own function"

    def __post_init__(self) -> None:
        if self._token is not _MINT:
            raise TypeError(self.refusal)

    def __reduce__(self) -> Any:  # copy.copy, copy.deepcopy and pickle would skip __init__
        raise TypeError(self.refusal)


def mint(cls: type[T], **fields: Any) -> T:
    """Make an instance without going through the refusing `__init__`."""
    made = object.__new__(cls)
    for name, value in fields.items():
        object.__setattr__(made, name, value)
    object.__setattr__(made, "_token", _MINT)
    return made


def is_minted(value: object, cls: type[MintedText]) -> bool:
    """True only for a genuine instance of exactly `cls` that `mint` made."""
    return type(value) is cls and getattr(value, "_token", None) is _MINT
