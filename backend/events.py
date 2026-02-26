from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

EventKind = Literal["log", "state", "recording"]


@dataclass(slots=True)
class AppEvent:
    kind: EventKind
    message: str = ""
    running: bool = False
    recording: bool = False
