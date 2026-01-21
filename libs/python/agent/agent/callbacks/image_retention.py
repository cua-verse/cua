"""
Image retention callback handler that limits the number of recent images in message history.
"""

import copy
from typing import Any, Dict, List, Optional

from .base import AsyncCallbackHandler


class ImageRetentionCallback(AsyncCallbackHandler):
    """
    Callback handler that applies image retention policy to limit the number
    of recent images in message history to prevent context window overflow.
    """

    def __init__(self, only_n_most_recent_images: Optional[int] = None):
        """
        Initialize the image retention callback.

        Args:
            only_n_most_recent_images: If set, only keep the N most recent images in message history
        """
        self.only_n_most_recent_images = only_n_most_recent_images

    async def on_llm_start(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Apply image retention policy to messages before sending to agent loop.

        Args:
            messages: List of message dictionaries

        Returns:
            List of messages with image retention policy applied
        """
        if self.only_n_most_recent_images is None:
            return messages

        return self._apply_image_retention(messages)

    def _apply_image_retention(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Apply image retention policy to keep only the N most recent images.

        Replaces image_url with "[omitted]" for older computer_call_output items,
        keeping only the most recent N images based on only_n_most_recent_images setting.
        Preserves all other message content including computer_call and reasoning items.

        Args:
            messages: List of message dictionaries

        Returns:
            List of messages with image retention policy applied
        """
        if self.only_n_most_recent_images is None:
            return messages

        # Gather indices of all computer_call_output messages that contain an image_url
        output_indices: List[int] = []
        for idx, msg in enumerate(messages):
            if isinstance(msg, dict) and msg.get("type") == "computer_call_output":
                out = msg.get("output")
                if isinstance(out, dict) and ("image_url" in out):
                    output_indices.append(idx)

        # Nothing to trim
        if len(output_indices) <= self.only_n_most_recent_images:
            return messages

        # Determine which outputs to keep (most recent N)
        keep_output_indices = set(output_indices[-self.only_n_most_recent_images :])

        # Create a deep copy of messages to modify
        modified_messages = copy.deepcopy(messages)

        for idx in output_indices:
            if idx in keep_output_indices:
                continue  # keep this image

            # Replace image_url with [omitted] instead of deleting the entire message
            msg = modified_messages[idx]
            if isinstance(msg, dict):
                output = msg.get("output", {})
                if isinstance(output, dict):
                    output["image_url"] = "[omitted]"

        return modified_messages
