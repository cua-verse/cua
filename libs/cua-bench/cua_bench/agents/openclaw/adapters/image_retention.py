"""E — ImageRetentionCallback extensions for the openclaw harness.

Two extensions on top of the SDK's ``ImageRetentionCallback``:

1. **call_id-based pairing** (legacy back-port for SDK pins predating the
   openclaw fork). The SDK assumed the ``computer_call`` that produced a
   ``computer_call_output`` was at ``idx - 1`` — broken when models emit
   interleaved ``function_call`` and ``computer_call`` items in the same
   turn (Opus 4.6 / ``computer_20251124`` class). Fork commit
   ``b420c6e8`` replaced the immediate-predecessor lookup with a
   backward scan keyed on ``call_id``. This subclass mirrors that.

2. **function_call shim coverage** (this is the live fix at every pin).
   The SDK's matcher only finds screenshots inside ``computer_call_output``
   items. Models that don't speak the native ``computer_call`` item
   (Claude, GPT-5.4, anything via OpenRouter) reach the computer tool
   through a function-call shim — the screenshot lands in a *separate
   user message* with ``image_url`` / ``input_image`` content blocks,
   not inside any ``*_output`` item. The SDK retention silently no-ops
   for that path. This subclass also walks user-message content lists,
   strips older image blocks, and drops messages whose content becomes
   empty after stripping.

   Unlike the native path (where the screenshot is the entire output, so
   removing it requires removing the producing ``computer_call`` too),
   the shim path's text status (``function_call_output``) is independent
   of the screenshot. We strip just the image and keep the action +
   status text, preserving the agent's audit trail of what it tried
   while shedding the heavy ~1.2K-token image.
"""

from __future__ import annotations

from typing import Any, Dict, List

from agent.callbacks.image_retention import ImageRetentionCallback


def _is_image_block(block: Any) -> bool:
    return (
        isinstance(block, dict)
        and block.get("type") in ("image_url", "input_image")
    )


class OpenClawImageRetentionCallback(ImageRetentionCallback):
    """ImageRetentionCallback that prunes both native and function-call shim screenshots."""

    def _apply_image_retention(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if self.only_n_most_recent_images is None:
            return messages

        n = self.only_n_most_recent_images

        # Index every image location across both paths, in message order.
        # Each entry: (msg_idx, kind, block_idx_or_None).
        # kind == "native_output"  → the entire computer_call_output is the image.
        # kind == "user_block"     → one image block within a user-message content list.
        locs: list[tuple[int, str, int | None]] = []
        for idx, msg in enumerate(messages):
            if msg.get("type") == "computer_call_output":
                out = msg.get("output")
                if isinstance(out, dict) and "image_url" in out:
                    locs.append((idx, "native_output", None))
                continue
            content = msg.get("content")
            if isinstance(content, list):
                for bidx, block in enumerate(content):
                    if _is_image_block(block):
                        locs.append((idx, "user_block", bidx))

        if len(locs) <= n:
            return messages

        drop = locs[:-n]

        # Native: remove the computer_call_output, its producing computer_call
        # (matched by call_id), and any preceding reasoning block.
        drop_native_indices = {idx for idx, kind, _ in drop if kind == "native_output"}
        to_remove: set[int] = set()
        for idx in drop_native_indices:
            to_remove.add(idx)
            output_call_id = messages[idx].get("call_id")
            for search_idx in range(idx - 1, -1, -1):
                if (
                    messages[search_idx].get("type") == "computer_call"
                    and messages[search_idx].get("call_id") == output_call_id
                ):
                    to_remove.add(search_idx)
                    r_idx = search_idx - 1
                    if r_idx >= 0 and messages[r_idx].get("type") == "reasoning":
                        to_remove.add(r_idx)
                    break

        # Shim: strip per-block from user messages. Messages whose content is
        # entirely image blocks (the typical post-action screenshot message)
        # become empty after stripping → drop the message entirely.
        drop_blocks_by_msg: dict[int, set[int]] = {}
        for idx, kind, bidx in drop:
            if kind == "user_block":
                drop_blocks_by_msg.setdefault(idx, set()).add(bidx)

        out: List[Dict[str, Any]] = []
        for i, msg in enumerate(messages):
            if i in to_remove:
                continue
            if i in drop_blocks_by_msg:
                stripped = [
                    b
                    for bi, b in enumerate(msg.get("content", []))
                    if bi not in drop_blocks_by_msg[i]
                ]
                if not stripped:
                    continue
                out.append({**msg, "content": stripped})
            else:
                out.append(msg)
        return out
