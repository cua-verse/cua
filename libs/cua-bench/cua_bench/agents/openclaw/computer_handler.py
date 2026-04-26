"""OpenClaw-specific cuaComputerHandler subclass.

Single source of truth for keypress semantics in the OpenClaw harness.
Previously duplicated in the orchestration repo's adapter (motivated by an
older CUA pin that lacked ``_normalize_key``); arrow-key normalization now
lives upstream in ``cuaComputerHandler``, so the only remaining override
is ``keypress`` — to fix the chord-vs-sequence ambiguity.

Why override ``keypress``:
    The OpenAI computer-use spec leaves ``keys=["right","right","down"]``
    ambiguous between chord (hold all keys) and sequence (press in order).
    Upstream cuaComputerHandler always routes a list of length > 1 through
    ``hotkey``, which collapses duplicates and produces unintended
    diagonal-key holds — silently breaking games (e.g. Magic Tower) and
    any app that expects discrete key events.

Convention used here:
    - String input (``"ctrl+shift+s"`` or legacy ``"ctrl-shift-s"``) → chord.
    - List input (``["right","right","down"]``) → sequence of independent
      presses, in order.
    - Single-element list (``["enter"]``) → ``press_key`` (unchanged).
"""

from __future__ import annotations

from typing import List, Union

from agent.computers.cua import cuaComputerHandler


class OpenClawComputerHandler(cuaComputerHandler):
    """cuaComputerHandler with sequential multi-key keypress."""

    async def keypress(self, keys: Union[List[str], str]) -> None:
        assert self.interface is not None
        if isinstance(keys, str):
            await super().keypress(keys)
            return
        for k in keys:
            await self.interface.press_key(self._normalize_key(k))
