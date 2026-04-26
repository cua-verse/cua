"""E — ImageRetentionCallback call-id matching back-port.

For CUA SDK pins that predate the openclaw fork, ``_apply_image_retention``
assumes the ``computer_call`` that produced a ``computer_call_output`` is
at ``idx - 1``. That holds for legacy adjacent-pattern transcripts (every
Anthropic-native and ``computer-use-preview`` model) but breaks when a
model emits interleaved ``function_call`` and ``computer_call`` items in
the same turn (Opus 4.6 / ``computer_20251124`` class), leaving the
matching ``computer_call`` orphaned when retention prunes screenshots.

The fork (commit ``b420c6e8``) replaces the immediate-predecessor lookup
with a backward scan keyed on ``call_id``. Pure addition for the
legacy-adjacent case (loop terminates on the first iteration), corrective
for the interleaved case.

This subclass mirrors the fork behavior. At pins that already include the
fix, the override is a no-op.
"""

from __future__ import annotations

from typing import Any, Dict, List

from agent.callbacks.image_retention import ImageRetentionCallback


class OpenClawImageRetentionCallback(ImageRetentionCallback):
    """ImageRetentionCallback that pairs outputs to calls by ``call_id``."""

    def _apply_image_retention(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if self.only_n_most_recent_images is None:
            return messages

        output_indices: List[int] = []
        for idx, msg in enumerate(messages):
            if msg.get("type") == "computer_call_output":
                out = msg.get("output")
                if isinstance(out, dict) and ("image_url" in out):
                    output_indices.append(idx)

        if len(output_indices) <= self.only_n_most_recent_images:
            return messages

        keep_output_indices = set(output_indices[-self.only_n_most_recent_images :])
        to_remove: set[int] = set()

        for idx in output_indices:
            if idx in keep_output_indices:
                continue

            to_remove.add(idx)

            # Match the producing computer_call by call_id, not by
            # position. Other items (function_call, function_call_output,
            # reasoning) may be interleaved between the call and its
            # output when the model emits both kinds in the same turn.
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

        return [m for i, m in enumerate(messages) if i not in to_remove]
