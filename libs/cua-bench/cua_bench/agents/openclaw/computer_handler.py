"""OpenClaw-specific cuaComputerHandler subclass.

Single source of truth for keypress semantics in the OpenClaw harness.

Why override ``keypress``:
    The OpenAI computer-use spec leaves ``keys=["right","right","down"]``
    ambiguous between chord (hold all keys) and sequence (press in order).
    Upstream cuaComputerHandler always routes a list of length > 1 through
    ``hotkey``, which collapses duplicates and produces unintended
    diagonal-key holds — silently breaking games (e.g. Magic Tower) and
    any app that expects discrete key events.

Why ship a local ``_normalize_key``:
    Older ``cua-verse/cua`` mainline pins (e.g. ``2a10d326``) do not define
    ``_normalize_key`` on ``cuaComputerHandler``; relying on the parent
    raised ``AttributeError`` the moment a list-keypress fires. Carrying
    the shim here keeps the openclaw harness working across both pins
    that ship the helper upstream and pins that don't.

Convention used here:
    - String input (``"ctrl+shift+s"`` or legacy ``"ctrl-shift-s"``) → chord.
    - List input (``["right","right","down"]``) → sequence of independent
      presses, in order.
    - Single-element list (``["enter"]``) → ``press_key`` (unchanged).
"""

from __future__ import annotations

from typing import List, Union

from agent.computers.cua import cuaComputerHandler


_KEY_MAPPING = {
    "ARROWUP": "up",
    "ARROWDOWN": "down",
    "ARROWLEFT": "left",
    "ARROWRIGHT": "right",
}


class OpenClawComputerHandler(cuaComputerHandler):
    """cuaComputerHandler with sequential multi-key keypress."""

    def _normalize_key(self, key: str) -> str:
        parent = getattr(super(), "_normalize_key", None)
        if callable(parent):
            return parent(key)
        return _KEY_MAPPING.get(key.upper(), key.lower())

    async def keypress(self, keys: Union[List[str], str]) -> None:
        assert self.interface is not None
        if isinstance(keys, str):
            await super().keypress(keys)
            return
        for k in keys:
            await self.interface.press_key(self._normalize_key(k))
