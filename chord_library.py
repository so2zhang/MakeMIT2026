"""Reusable chord voicing library for glove playback."""

from dataclasses import dataclass
from typing import List, Optional


@dataclass(frozen=True)
class ChordVoicing:
    name: str
    notes: List[int]


DEFAULT_SEQUENCE: List[ChordVoicing] = [
    ChordVoicing("vim9", [69, 72, 76, 79]),
    ChordVoicing("vim7", [69, 72, 76, 79]),
    ChordVoicing("Vsus4", [67, 72, 74]),
    ChordVoicing("IVmaj7", [60, 64, 67]),
    ChordVoicing("Imaj7", [60, 64, 67, 71]),
]


class ChordSequencePlayer:
    """Cycles through a fixed sequence of named chord voicings."""

    def __init__(self, sequence: Optional[List[ChordVoicing]] = None):
        self.sequence = sequence or DEFAULT_SEQUENCE
        self.index = 0

    def current(self) -> ChordVoicing:
        return self.sequence[self.index % len(self.sequence)]

    def next(self) -> ChordVoicing:
        self.index = (self.index + 1) % len(self.sequence)
        return self.current()
