"""Tests for US-OC-014: Transcript Fidelity — Capture on_llm_start Messages.

Covers:
- _sanitize_api_messages_for_storage: base64 replaced, structure preserved
- ContextOverflowCallback.on_llm_start stores last_api_messages
- SessionManager.append_message with api_messages persists to JSONL
- Round-trip: write entry with apiMessages, load_history, verify present
"""

import asyncio
import base64
import json
import os
import tempfile
from pathlib import Path

from cua_bench.agents.openclaw.context import (
    ContextOverflowCallback,
    _sanitize_api_messages_for_storage,
)
from cua_bench.agents.openclaw.session import SessionManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_base64_image(size: int = 200) -> str:
    """Create a fake base64 data URL of approximately `size` chars."""
    raw = base64.b64encode(os.urandom(size)).decode()
    return f"data:image/png;base64,{raw}"


def _make_computer_call_output(call_id: str, image_url: str) -> dict:
    """Build a computer_call_output item with an input_image."""
    return {
        "type": "computer_call_output",
        "call_id": call_id,
        "output": {
            "type": "input_image",
            "image_url": image_url,
        },
    }


def _make_trajectory_dir(num_screenshots: int = 3) -> Path:
    """Create a temp trajectory dir with numbered screenshot files."""
    tmpdir = Path(tempfile.mkdtemp())
    for i in range(num_screenshots):
        turn_dir = tmpdir / f"turn_{i:03d}"
        turn_dir.mkdir()
        screenshot = turn_dir / f"{i:03d}_screenshot_after.png"
        screenshot.write_bytes(b"fake png data")
    return tmpdir


# ---------------------------------------------------------------------------
# _sanitize_api_messages_for_storage
# ---------------------------------------------------------------------------

class TestSanitizeApiMessages:
    """Test _sanitize_api_messages_for_storage."""

    def test_replaces_base64_with_file_paths(self):
        """Base64 image data in computer_call_output should be replaced with file:// paths."""
        traj = _make_trajectory_dir(2)
        b64_1 = _make_base64_image()
        b64_2 = _make_base64_image()
        messages = [
            _make_computer_call_output("call-1", b64_1),
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "hello"}]},
            _make_computer_call_output("call-2", b64_2),
        ]

        result = _sanitize_api_messages_for_storage(messages, traj)

        # Original unchanged
        assert messages[0]["output"]["image_url"] == b64_1

        # Result has file:// paths
        assert result[0]["output"]["image_url"].startswith("file://")
        assert "screenshot_after.png" in result[0]["output"]["image_url"]
        assert result[2]["output"]["image_url"].startswith("file://")

        # Text content preserved
        assert result[1]["content"][0]["text"] == "hello"

    def test_fallback_placeholder_when_no_trajectory(self):
        """Without trajectory_dir, base64 is replaced with [image:NNNNchars] placeholder."""
        b64 = _make_base64_image(300)
        messages = [_make_computer_call_output("call-1", b64)]

        result = _sanitize_api_messages_for_storage(messages, None)

        url = result[0]["output"]["image_url"]
        assert url.startswith("[image:")
        assert url.endswith("chars]")
        assert "base64" not in url

    def test_non_image_content_intact(self):
        """Text, function_call, and other items pass through unchanged."""
        messages = [
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "do something"}]},
            {"type": "function_call", "call_id": "fc-1", "name": "memory_write", "arguments": '{"content": "hi"}'},
            {"type": "function_call_output", "call_id": "fc-1", "output": "ok"},
        ]

        result = _sanitize_api_messages_for_storage(messages, None)

        assert result[0] == messages[0]
        assert result[1] == messages[1]
        assert result[2] == messages[2]

    def test_deep_copy_does_not_mutate_original(self):
        """Sanitization must not modify the original messages list."""
        b64 = _make_base64_image()
        messages = [_make_computer_call_output("call-1", b64)]
        original_url = messages[0]["output"]["image_url"]

        _sanitize_api_messages_for_storage(messages, None)

        assert messages[0]["output"]["image_url"] == original_url

    def test_multiple_images_map_chronologically(self):
        """The Nth image in messages maps to the Nth trajectory screenshot."""
        traj = _make_trajectory_dir(3)
        messages = [
            _make_computer_call_output("c1", _make_base64_image()),
            _make_computer_call_output("c2", _make_base64_image()),
            _make_computer_call_output("c3", _make_base64_image()),
        ]

        result = _sanitize_api_messages_for_storage(messages, traj)

        paths = [r["output"]["image_url"] for r in result]
        # All should be file:// paths
        assert all(p.startswith("file://") for p in paths)
        # All should be different
        assert len(set(paths)) == 3
        # Should contain turn_000, turn_001, turn_002
        assert "turn_000" in paths[0]
        assert "turn_001" in paths[1]
        assert "turn_002" in paths[2]


# ---------------------------------------------------------------------------
# ContextOverflowCallback.on_llm_start captures messages
# ---------------------------------------------------------------------------

class TestContextOverflowCallbackCapture:
    """Test that on_llm_start stores last_api_messages."""

    def test_captures_messages(self):
        """After on_llm_start, last_api_messages should be set."""
        cb = ContextOverflowCallback(context_window=200_000, model="test")
        assert cb.last_api_messages is None

        messages = [
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]},
        ]
        asyncio.get_event_loop().run_until_complete(cb.on_llm_start(messages))

        assert cb.last_api_messages is not None
        assert len(cb.last_api_messages) == 1
        assert cb.last_api_messages[0]["content"][0]["text"] == "hi"

    def test_captures_sanitized_images(self):
        """Base64 images in captured messages should be sanitized."""
        traj = _make_trajectory_dir(1)
        cb = ContextOverflowCallback(
            context_window=200_000, model="test", trajectory_dir=traj
        )

        b64 = _make_base64_image()
        messages = [_make_computer_call_output("call-1", b64)]
        asyncio.get_event_loop().run_until_complete(cb.on_llm_start(messages))

        captured = cb.last_api_messages
        assert captured is not None
        assert captured[0]["output"]["image_url"].startswith("file://")

    def test_updates_on_each_call(self):
        """Each on_llm_start call should update last_api_messages."""
        cb = ContextOverflowCallback(context_window=200_000, model="test")

        messages_1 = [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "first"}]}]
        messages_2 = [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "second"}]}]

        asyncio.get_event_loop().run_until_complete(cb.on_llm_start(messages_1))
        assert cb.last_api_messages[0]["content"][0]["text"] == "first"

        asyncio.get_event_loop().run_until_complete(cb.on_llm_start(messages_2))
        assert cb.last_api_messages[0]["content"][0]["text"] == "second"


# ---------------------------------------------------------------------------
# SessionManager.append_message with api_messages
# ---------------------------------------------------------------------------

class TestSessionManagerApiMessages:
    """Test api_messages parameter in SessionManager.append_message."""

    def _make_session_mgr(self, tmp_path: Path) -> SessionManager:
        mgr = SessionManager(task_id="test-task", base_dir=tmp_path)
        mgr.init_session(model="test-model")
        return mgr

    def test_api_messages_persisted(self, tmp_path):
        """api_messages should appear as apiMessages in the JSONL entry."""
        mgr = self._make_session_mgr(tmp_path)
        api_msgs = [{"type": "message", "role": "user", "content": "test"}]

        mgr.append_message(
            "assistant",
            "hello",
            api_messages=api_msgs,
        )

        # Read the last line of transcript
        lines = mgr.transcript_path.read_text().strip().splitlines()
        last = json.loads(lines[-1])
        assert "apiMessages" in last
        assert last["apiMessages"] == api_msgs

    def test_no_api_messages_when_none(self, tmp_path):
        """When api_messages is None, apiMessages key should be absent."""
        mgr = self._make_session_mgr(tmp_path)

        mgr.append_message("assistant", "hello")

        lines = mgr.transcript_path.read_text().strip().splitlines()
        last = json.loads(lines[-1])
        assert "apiMessages" not in last

    def test_round_trip_with_load_history(self, tmp_path):
        """apiMessages should survive write→load_history round-trip."""
        mgr = self._make_session_mgr(tmp_path)
        api_msgs = [
            {"type": "computer_call_output", "call_id": "cc-1", "output": {"type": "input_image", "image_url": "file:///path/to/screenshot.png"}},
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "I see the screen"}]},
        ]

        mgr.append_message(
            "assistant",
            [{"type": "text", "text": "I see the screen"}],
            api_messages=api_msgs,
        )

        entries = mgr.load_history()
        # Find the message entry (skip session header)
        msg_entries = [e for e in entries if e.type == "message"]
        assert len(msg_entries) == 1
        assert "apiMessages" in msg_entries[0].data
        assert msg_entries[0].data["apiMessages"] == api_msgs
