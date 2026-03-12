"""
SessionManager — Cross-run session persistence for the OpenClaw agent harness.

Reproduces OpenClaw's session persistence (openclaw/src/config/sessions/transcript.ts)
adapted for CUA's single-task benchmark context. Keeps 3 of 10 OpenClaw JSONL entry types
(session, message, compaction) and drops 7 UI/multi-model types irrelevant to CUA.

Key differences from OpenClaw:
- 3 of 10 entry types (session, message, compaction)
- Single JSONL file per task (session headers mark run boundaries)
- Run numbers derived from transcript session headers (not stored in state.json)
- Task-scoped: sessions_dir/<task_id>/ vs OpenClaw's agent-scoped routing keys
"""

import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_BASE_DIR = "openclaw_sessions"


@dataclass
class TokenUsage:
    """Cumulative token usage tracking.

    Uses input_tokens/output_tokens naming to match CUA SDK (OpenAI Responses API format).
    Cache token fields track Anthropic prompt caching usage.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0
    context_tokens: int = 0

    def accumulate(
        self,
        input_tokens: int,
        output_tokens: int,
        *,
        cache_read: int = 0,
        cache_write: int = 0,
        context_tokens: int = 0,
    ) -> None:
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.cache_read += cache_read
        self.cache_write += cache_write
        self.context_tokens += context_tokens

    def to_dict(self) -> dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read": self.cache_read,
            "cache_write": self.cache_write,
            "context_tokens": self.context_tokens,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TokenUsage":
        return cls(
            input_tokens=data.get("input_tokens", 0),
            output_tokens=data.get("output_tokens", 0),
            cache_read=data.get("cache_read", 0),
            cache_write=data.get("cache_write", 0),
            context_tokens=data.get("context_tokens", 0),
        )


@dataclass
class SessionState:
    """Cross-run metadata persisted in state.json.

    Run numbers are derived from transcript session headers, not stored here.
    """

    task_id: str
    step_count: int = 0
    total_tokens: TokenUsage = field(default_factory=TokenUsage)
    compaction_count: int = 0
    compaction_summaries: list[str] = field(default_factory=list)
    model: str = ""
    system_prompt_report: dict[str, Any] | None = None
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "task_id": self.task_id,
            "step_count": self.step_count,
            "total_tokens": self.total_tokens.to_dict(),
            "compaction_count": self.compaction_count,
            "compaction_summaries": self.compaction_summaries,
            "model": self.model,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        if self.system_prompt_report is not None:
            d["system_prompt_report"] = self.system_prompt_report
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SessionState":
        tokens_data = data.get("total_tokens", {})
        return cls(
            task_id=data["task_id"],
            step_count=data.get("step_count", 0),
            total_tokens=TokenUsage.from_dict(tokens_data),
            compaction_count=data.get("compaction_count", 0),
            compaction_summaries=list(data.get("compaction_summaries", [])),
            model=data.get("model", ""),
            system_prompt_report=data.get("system_prompt_report"),
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
        )


@dataclass
class TranscriptEntry:
    """A single JSONL transcript entry with parentId chain.

    Discriminated by `type`:
    - session: version, task_id, run_number, model
    - message: message.{role, content, usage?, stopReason?}
    - compaction: summary, firstKeptEntryId, tokensBefore
    """

    type: str
    id: str
    parent_id: str | None
    timestamp: str
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "type": self.type,
            "id": self.id,
            "parentId": self.parent_id,
            "timestamp": self.timestamp,
        }
        entry.update(self.data)
        return entry

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TranscriptEntry":
        core_keys = {"type", "id", "parentId", "timestamp"}
        extra = {k: v for k, v in data.items() if k not in core_keys}
        return cls(
            type=data["type"],
            id=data["id"],
            parent_id=data.get("parentId"),
            timestamp=data["timestamp"],
            data=extra,
        )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id(prefix: str = "entry") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class SessionManager:
    """Manages cross-run session state and JSONL transcripts for a single task.

    Storage layout:
        <base_dir>/<task_id>/state.json       — cross-run metadata
        <base_dir>/<task_id>/transcript.jsonl  — append-only conversation log

    Uses the None-sentinel pattern from MemoryStore for base_dir defaulting.
    """

    def __init__(self, task_id: str, base_dir: str | Path | None = None):
        self.task_id = task_id
        self._base_dir = Path(base_dir) if base_dir is not None else Path(DEFAULT_BASE_DIR)
        self._state: SessionState | None = None
        self._last_entry_id: str | None = None

    @property
    def task_dir(self) -> Path:
        return self._base_dir / self.task_id

    @property
    def state_path(self) -> Path:
        return self.task_dir / "state.json"

    @property
    def transcript_path(self) -> Path:
        return self.task_dir / "transcript.jsonl"

    def load_state(self) -> SessionState | None:
        """Load state from state.json. Returns None if missing or corrupt."""
        if not self.state_path.exists():
            return None
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            return SessionState.from_dict(data)
        except (json.JSONDecodeError, KeyError, OSError):
            return None

    def save_state(self) -> None:
        """Persist current state to state.json."""
        if self._state is None:
            return
        self._state.updated_at = _now_iso()
        self.task_dir.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(
            json.dumps(self._state.to_dict(), indent=2) + "\n",
            encoding="utf-8",
        )

    def init_session(self, model: str = "") -> SessionState:
        """Initialize a new run session.

        Loads existing state (if any), resets step_count, sets model,
        preserves cumulative tokens and compaction summaries, and appends a
        session header entry to transcript.jsonl.

        Run number is derived by counting existing session headers in the
        transcript rather than being stored in state.json.

        Returns the updated SessionState.
        """
        existing = self.load_state()
        now = _now_iso()

        if existing is not None:
            self._state = existing
            self._state.step_count = 0
            self._state.model = model
            self._state.updated_at = now
        else:
            self._state = SessionState(
                task_id=self.task_id,
                step_count=0,
                model=model,
                created_at=now,
                updated_at=now,
            )

        self.save_state()

        # Derive run_number from transcript session headers
        run_number = self._count_session_headers() + 1

        # Append session header to transcript
        entry = TranscriptEntry(
            type="session",
            id=_new_id("sess"),
            parent_id=None,
            timestamp=now,
            data={
                "version": 1,
                "task_id": self.task_id,
                "run_number": run_number,
                "model": model,
            },
        )
        self._append_entry(entry)
        self._last_entry_id = entry.id

        return self._state

    def append_message(
        self,
        role: str,
        content: str | list[dict[str, Any]],
        usage: dict[str, Any] | None = None,
        stop_reason: str | None = None,
    ) -> TranscriptEntry:
        """Append a message entry to the transcript.

        Mirrors OpenClaw's transcript format where content is an array of typed blocks:
        text, toolCall, toolResult, image, computer_call, etc.

        Args:
            role: "user", "assistant", or "toolResult"
            content: Text string (auto-wrapped as [{type: "text", text: ...}])
                    or a content array of typed blocks
            usage: Optional dict with input/output/total/cost keys
            stop_reason: Optional stop reason (e.g. "tool_use", "end_turn")

        Returns the created TranscriptEntry.
        """
        if isinstance(content, str):
            content_array = [{"type": "text", "text": content}]
        else:
            content_array = content

        message_data: dict[str, Any] = {"role": role, "content": content_array}
        if usage is not None:
            message_data["usage"] = usage
        if stop_reason is not None:
            message_data["stopReason"] = stop_reason

        entry = TranscriptEntry(
            type="message",
            id=_new_id("msg"),
            parent_id=self._last_entry_id,
            timestamp=_now_iso(),
            data={"message": message_data},
        )
        self._append_entry(entry)
        self._last_entry_id = entry.id
        return entry

    def append_compaction(
        self,
        summary: str,
        first_kept_entry_id: str,
        tokens_before: int,
    ) -> TranscriptEntry:
        """Append a compaction entry to the transcript and update state.

        Args:
            summary: The compaction summary text
            first_kept_entry_id: ID of the first entry kept after compaction
            tokens_before: Token count before compaction

        Returns the created TranscriptEntry.
        """
        entry = TranscriptEntry(
            type="compaction",
            id=_new_id("cmp"),
            parent_id=self._last_entry_id,
            timestamp=_now_iso(),
            data={
                "summary": summary,
                "firstKeptEntryId": first_kept_entry_id,
                "tokensBefore": tokens_before,
            },
        )
        self._append_entry(entry)
        self._last_entry_id = entry.id

        # Update state
        if self._state is not None:
            self._state.compaction_count += 1
            self._state.compaction_summaries.append(summary)
            self.save_state()

        return entry

    def load_history(self, run_number: int | None = None) -> list[TranscriptEntry]:
        """Load transcript entries, optionally filtered to a specific run.

        Args:
            run_number: If provided, only return entries from this run.
                       If None, return all entries.

        Returns list of TranscriptEntry objects.
        """
        if not self.transcript_path.exists():
            return []

        entries: list[TranscriptEntry] = []
        current_run: int | None = None
        in_target_run = run_number is None  # If no filter, include everything

        for line in self.transcript_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue

            entry = TranscriptEntry.from_dict(data)

            if entry.type == "session":
                current_run = entry.data.get("run_number")
                if run_number is not None:
                    in_target_run = current_run == run_number

            if in_target_run:
                entries.append(entry)

        return entries

    def update_tokens(self, input_tokens: int, output_tokens: int) -> None:
        """Accumulate token usage and persist."""
        if self._state is not None:
            self._state.total_tokens.accumulate(input_tokens, output_tokens)
            self.save_state()

    def update_step_count(self, step: int) -> None:
        """Update the current step count and persist."""
        if self._state is not None:
            self._state.step_count = step
            self.save_state()

    def add_compaction_summary(self, summary: str) -> None:
        """Add a compaction summary to state.json (without a transcript entry)."""
        if self._state is not None:
            self._state.compaction_count += 1
            self._state.compaction_summaries.append(summary)
            self.save_state()

    def get_compaction_summaries(self) -> list[str]:
        """Return compaction summaries from state."""
        if self._state is not None:
            return list(self._state.compaction_summaries)
        loaded = self.load_state()
        if loaded is not None:
            return list(loaded.compaction_summaries)
        return []

    def set_system_prompt_report(self, report: dict[str, Any]) -> None:
        """Store a system prompt report in state.json."""
        if self._state is not None:
            self._state.system_prompt_report = report
            self.save_state()

    def _count_session_headers(self) -> int:
        """Count the number of session header entries in the transcript."""
        if not self.transcript_path.exists():
            return 0
        count = 0
        for line in self.transcript_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if data.get("type") == "session":
                count += 1
        return count

    def _append_entry(self, entry: TranscriptEntry) -> None:
        """Append a single JSONL line to the transcript file."""
        self.task_dir.mkdir(parents=True, exist_ok=True)
        with open(self.transcript_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry.to_dict()) + "\n")


def build_system_prompt_report(
    *,
    system_prompt: str,
    context_files: list[Any] | None = None,
    tool_summaries: dict[str, str] | None = None,
    tools: list[Any] | None = None,
    source: str = "run",
) -> dict[str, Any]:
    """Build a report describing the system prompt composition.

    Measures total prompt size, splits project vs non-project context,
    catalogs injected files and tool schemas. Uses duck-typing for tool
    objects to avoid importing BaseTool.

    Based on OpenClaw's system prompt reporting for observability.
    """
    total_chars = len(system_prompt)

    # Split project vs non-project context at "# Project Context" header
    project_marker = "# Project Context"
    marker_pos = system_prompt.find(project_marker)
    if marker_pos >= 0:
        project_context_chars = total_chars - marker_pos
        non_project_context_chars = marker_pos
    else:
        project_context_chars = 0
        non_project_context_chars = total_chars

    # Injected files
    injected_files: list[dict[str, Any]] = []
    if context_files is not None:
        for cf in context_files:
            raw_content = getattr(cf, "content", "")
            raw_chars = len(raw_content) if raw_content else 0
            name = getattr(cf, "name", str(cf))

            # Measure how many chars actually appear in the prompt
            if raw_content and raw_content in system_prompt:
                injected_chars = len(raw_content)
            elif name in system_prompt:
                # Content was truncated; measure what's between file markers
                injected_chars = raw_chars  # fallback
            else:
                injected_chars = 0

            injected_files.append({
                "name": name,
                "raw_chars": raw_chars,
                "injected_chars": injected_chars,
                "truncated": injected_chars < raw_chars,
            })

    # Tools
    tool_entries: list[dict[str, Any]] = []
    if tools is not None:
        for tool in tools:
            name = getattr(tool, "name", str(tool))
            summary_chars = len(tool_summaries.get(name, "")) if tool_summaries else 0

            entry: dict[str, Any] = {"name": name, "summary_chars": summary_chars}

            # Duck-typed schema extraction
            if hasattr(tool, "parameters"):
                params = tool.parameters
                if isinstance(params, dict):
                    schema_str = json.dumps(params)
                    entry["schema_chars"] = len(schema_str)
                    props = params.get("properties", {})
                    entry["properties_count"] = len(props) if isinstance(props, dict) else 0
                else:
                    entry["schema_chars"] = 0
                    entry["properties_count"] = 0

            tool_entries.append(entry)
    elif tool_summaries is not None:
        for name, summary in tool_summaries.items():
            tool_entries.append({
                "name": name,
                "summary_chars": len(summary),
            })

    return {
        "source": source,
        "generated_at": time.time(),
        "system_prompt": {
            "chars": total_chars,
            "project_context_chars": project_context_chars,
            "non_project_context_chars": non_project_context_chars,
        },
        "injected_files": injected_files,
        "tools": {
            "entries": tool_entries,
        },
    }
