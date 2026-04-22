"""Delegation tools — BaseTool subclasses that spawn subagent runs (US-SUB-005).

Three tools are exposed to the main agent:
  - ``delegate_general``: async one-shot planning/analysis subagent driven by
    the US-SUB-008 ``GeneralSubagentSession`` persistent engine. Returns
    immediately with ``{"status": "accepted", "run_id": ...}``; the final
    result is delivered later as a ``[Subagent Result]`` user message via
    ``OpenClawComputerAgent._drain_completions``.
  - ``delegate_gui``: blocking vision-to-action relay driving the VM via the
    US-SUB-004 ``run_gui_subagent`` loop. Returns the final summary
    synchronously so the main agent can resume with fresh VM state.
  - ``subagents``: ``list`` active/recent runs or ``kill`` a runaway general
    subagent via ``registry.kill_run``.

Reference:
  ``openclaw/src/agents/tools/sessions-spawn-tool.ts`` (spawn-tool shape),
  ``openclaw/src/agents/tools/subagents-tool.ts`` (list/kill actions).

Design notes:
  * ``BaseTool.call()`` is synchronous. ``DelegateGeneralTool`` runs inside an
    active asyncio event loop (the CUA agent awaits tool calls), so it
    schedules a task with ``asyncio.get_running_loop().create_task`` and
    returns immediately. ``DelegateGUITool`` needs to block until the relay
    loop finishes, so it adopts the ``ThreadPoolExecutor + asyncio.run``
    pattern from ``AnalyzeImageTool.call`` (``analyze_image.py:144-168``).
  * All three tools degrade gracefully: ``DelegateGeneralTool`` returns a
    ``rejected`` payload when the registry refuses a spawn (concurrency cap);
    ``DelegateGUITool`` returns ``{"status": "error", ...}`` when the relay
    coroutine raises; ``SubagentsTool.kill`` reports ``noop``/``error`` for
    terminal/unknown targets.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Union

from agent.tools.base import BaseTool, register_tool

from .memory import MemoryStore
from .subagent_general import DEFAULT_MAX_STEPS as GENERAL_DEFAULT_MAX_STEPS
from .subagent_general import run_general_subagent
from .subagent_gui import DEFAULT_MAX_STEPS as GUI_DEFAULT_MAX_STEPS
from .subagent_gui import DEFAULT_MODEL as GUI_DEFAULT_MODEL
from .subagent_gui import run_gui_subagent
from .subagent_registry import (
    SubagentLimitError,
    SubagentRegistry,
    SubagentStatus,
    SubagentType,
)
from .subagent_session import _encode_image_url_from_path

DELEGATE_GENERAL_DEFAULT_MAX_STEPS = GENERAL_DEFAULT_MAX_STEPS

_POST_DELEGATION_TEXT = "[VM state after GUI delegation]"
_logger = logging.getLogger(__name__)

_ACCEPTED_NOTE = (
    "persistent session — result auto-announces when complete; do not poll"
)


# ---------------------------------------------------------------------------
# delegate_general
# ---------------------------------------------------------------------------


@register_tool("delegate_general")
class DelegateGeneralTool(BaseTool):
    """Spawn an async general subagent session (planning/analysis, no VM access)."""

    def __init__(
        self,
        registry: SubagentRegistry,
        tools: list,
        memory_store: MemoryStore,
        default_model: str,
        summary_model: str,
        parent_session_dir: Path,
        thinking_params: dict[str, Any] | None = None,
        cfg: dict | None = None,
    ) -> None:
        self._registry = registry
        self._tools = tools
        self._memory_store = memory_store
        self._default_model = default_model
        self._summary_model = summary_model
        self._parent_session_dir = Path(parent_session_dir)
        self._thinking_params = thinking_params
        super().__init__(cfg)

    @property
    def description(self) -> str:
        return (
            "Spawn an asynchronous *general* subagent to work on a focused "
            "planning/analysis/memory task. The subagent has NO direct VM "
            "access — it can only use memory tools and LLM reasoning. "
            "Returns immediately with a run_id; the final result is "
            "announced later as a '[Subagent Result]' user message. "
            "DO NOT poll with `subagents(list)` — results auto-announce."
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "What the subagent should accomplish.",
                },
                "model": {
                    "type": "string",
                    "description": (
                        "Optional litellm model override "
                        "(defaults to the main agent's model)."
                    ),
                },
                "max_steps": {
                    "type": "integer",
                    "description": (
                        "Safety rail for the subagent's loop (default 50). "
                        "The session compacts its own context and typically "
                        "completes well before this cap."
                    ),
                },
                "label": {
                    "type": "string",
                    "description": "Optional human-readable label for observability.",
                },
                "screenshot_paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Optional absolute paths to PNG screenshots to "
                        "attach to the subagent's initial message as vision "
                        "input. Use this to delegate 'analyze this frame' "
                        "work. Paths are the local files the main agent has "
                        "seen via '[Screenshot saved to: ...]' hints."
                    ),
                },
            },
            "required": ["task"],
        }

    def call(self, params: Union[str, dict], **kwargs) -> dict:
        params_dict = self._verify_json_format_args(params)

        task = params_dict.get("task", "")
        if not isinstance(task, str) or not task.strip():
            return {
                "status": "error",
                "reason": "task must be a non-empty string",
            }

        model = params_dict.get("model") or self._default_model
        max_steps = int(
            params_dict.get("max_steps", DELEGATE_GENERAL_DEFAULT_MAX_STEPS)
        )
        label = params_dict.get("label", "") or ""
        screenshot_paths_raw = params_dict.get("screenshot_paths") or []
        screenshot_paths: list[str] | None = (
            [p for p in screenshot_paths_raw if isinstance(p, str) and p]
            if isinstance(screenshot_paths_raw, list)
            else None
        )

        try:
            run = self._registry.register(
                type=SubagentType.GENERAL,
                task=task,
                label=label,
                model=model,
            )
        except SubagentLimitError:
            return {
                "status": "rejected",
                "reason": "max concurrent subagents reached",
            }

        coro = run_general_subagent(
            task=task,
            model=model,
            tools=self._tools,
            registry=self._registry,
            run_id=run.run_id,
            summary_model=self._summary_model,
            parent_session_dir=self._parent_session_dir,
            memory_store=self._memory_store,
            max_steps=max_steps,
            thinking_params=self._thinking_params,
            initial_screenshot_paths=screenshot_paths,
        )

        loop = asyncio.get_running_loop()
        task_handle = loop.create_task(coro)
        self._registry.attach_task(run.run_id, task_handle)

        return {
            "status": "accepted",
            "run_id": run.run_id,
            "note": _ACCEPTED_NOTE,
        }


# ---------------------------------------------------------------------------
# delegate_gui
# ---------------------------------------------------------------------------


@register_tool("delegate_gui")
class DelegateGUITool(BaseTool):
    """Spawn a blocking GUI subagent (vision-to-action relay on the VM)."""

    def __init__(
        self,
        registry: SubagentRegistry,
        session: Any,
        parent_session_dir: Path,
        default_model: str = GUI_DEFAULT_MODEL,
        thinking_params: dict[str, Any] | None = None,
        memory_store: MemoryStore | None = None,
        cfg: dict | None = None,
    ) -> None:
        self._registry = registry
        self._session = session
        self._parent_session_dir = Path(parent_session_dir)
        self._default_model = default_model
        self._thinking_params = thinking_params
        self._memory_store = memory_store
        super().__init__(cfg)

    @property
    def description(self) -> str:
        return (
            "Spawn a *GUI automation* subagent driven by a vision model. "
            "Returns immediately with a run_id; the subagent takes over "
            "the VM for a bounded number of steps. When finished, the "
            "result is announced as a '[Subagent Result]' user message "
            "followed by a fresh VM screenshot. DO NOT poll — results "
            "auto-announce. While the GUI subagent is running, the VM is "
            "occupied — do not call delegate_gui again or use computer "
            "directly until it completes."
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "instruction": {
                    "type": "string",
                    "description": "Self-contained GUI task (e.g. 'open Notepad').",
                },
                "model": {
                    "type": "string",
                    "description": (
                        f"Optional litellm model override "
                        f"(default: '{GUI_DEFAULT_MODEL}')."
                    ),
                },
                "max_steps": {
                    "type": "integer",
                    "description": (
                        f"Safety rail for the relay loop "
                        f"(default {GUI_DEFAULT_MAX_STEPS})."
                    ),
                },
                "label": {
                    "type": "string",
                    "description": "Optional human-readable label for observability.",
                },
            },
            "required": ["instruction"],
        }

    def call(self, params: Union[str, dict], **kwargs) -> dict:
        params_dict = self._verify_json_format_args(params)

        instruction = params_dict.get("instruction", "")
        if not isinstance(instruction, str) or not instruction.strip():
            return {
                "status": "error",
                "reason": "instruction must be a non-empty string",
            }

        model = params_dict.get("model") or self._default_model
        max_steps = int(params_dict.get("max_steps", GUI_DEFAULT_MAX_STEPS))
        label = params_dict.get("label", "") or ""

        run = self._registry.register(
            type=SubagentType.GUI,
            task=instruction,
            label=label,
            model=model,
        )

        async def _drive() -> None:
            try:
                await run_gui_subagent(
                    instruction=instruction,
                    session=self._session,
                    registry=self._registry,
                    run_id=run.run_id,
                    model=model,
                    max_steps=max_steps,
                    thinking_params=self._thinking_params,
                    parent_session_dir=self._parent_session_dir,
                    memory_store=self._memory_store,
                )
                # run_gui_subagent already calls registry.complete()
            except Exception as e:
                # run_gui_subagent already calls registry.fail() before raising
                _logger.warning("GUI subagent %s failed: %s", run.run_id, e)
                return

            try:
                post_shot = await self._session.screenshot()
            except Exception as post_exc:
                _logger.warning(
                    "post-delegation screenshot failed for run %s: %s",
                    run.run_id,
                    post_exc,
                )
                post_shot = None

            if isinstance(post_shot, (bytes, bytearray)) and post_shot:
                self._enqueue_post_delegation(run.run_id, bytes(post_shot))

        loop = asyncio.get_running_loop()
        task_handle = loop.create_task(_drive())
        self._registry.attach_task(run.run_id, task_handle)

        return {
            "status": "accepted",
            "run_id": run.run_id,
            "note": _ACCEPTED_NOTE,
        }

    def _enqueue_post_delegation(self, run_id: str, png_bytes: bytes) -> None:
        """Persist the fresh screenshot and enqueue a user message for the main agent.

        Writes ``<parent_session_dir>/subagents/<run_id>/post_delegation.png``
        for trajectory inspection, then pushes a pre-built
        ``{role: user, content: [text, image_url]}`` dict to the registry's
        post-delegation queue for ``_drain_post_delegation`` to fold into the
        next main-agent turn (US-SUB-006).
        """
        try:
            target_dir = self._parent_session_dir / "subagents" / run_id
            target_dir.mkdir(parents=True, exist_ok=True)
            target_path = target_dir / "post_delegation.png"
            target_path.write_bytes(png_bytes)
        except OSError as exc:
            _logger.warning(
                "could not persist post-delegation screenshot for %s: %s",
                run_id,
                exc,
            )
            return

        block = _encode_image_url_from_path(str(target_path))
        if block is None:
            _logger.warning(
                "could not encode post-delegation screenshot for %s", run_id
            )
            return

        message = {
            "role": "user",
            "content": [
                {"type": "text", "text": _POST_DELEGATION_TEXT},
                block,
            ],
        }
        self._registry.enqueue_post_delegation(message)


# ---------------------------------------------------------------------------
# subagents (list / kill)
# ---------------------------------------------------------------------------


@register_tool("subagents")
class SubagentsTool(BaseTool):
    """Inspect or cancel active subagent runs (list / kill)."""

    def __init__(
        self,
        registry: SubagentRegistry,
        cfg: dict | None = None,
    ) -> None:
        self._registry = registry
        super().__init__(cfg)

    @property
    def description(self) -> str:
        return (
            "Inspect or cancel subagent runs. "
            "action='list' returns active (running/pending) and recent "
            "(terminal) runs for debugging. action='kill' cancels a runaway "
            "general subagent. Prefer letting general subagents finish on "
            "their own — kill only when you're sure a run is stuck."
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list", "kill"],
                    "description": "'list' (default) or 'kill'.",
                },
                "target": {
                    "type": "string",
                    "description": "Required for action='kill'. The run_id to cancel.",
                },
            },
            "required": [],
        }

    def call(self, params: Union[str, dict], **kwargs) -> dict:
        params_dict = self._verify_json_format_args(params)
        action = params_dict.get("action", "list")

        if action == "list":
            active: list[dict] = []
            recent: list[dict] = []
            for run in self._registry.list_runs():
                if run.status in (SubagentStatus.PENDING, SubagentStatus.RUNNING):
                    active.append(run.to_dict())
                else:
                    recent.append(run.to_dict())
            return {"status": "ok", "active": active, "recent": recent}

        if action == "kill":
            target = params_dict.get("target")
            if not isinstance(target, str) or not target:
                return {
                    "status": "error",
                    "reason": "target run_id is required for action='kill'",
                }
            run = self._registry.get_run(target)
            if run is None:
                return {"status": "error", "reason": "unknown run_id"}
            if run.status in (
                SubagentStatus.COMPLETE,
                SubagentStatus.ERROR,
                SubagentStatus.KILLED,
            ):
                return {"status": "noop", "reason": "already terminal"}
            self._registry.kill_run(target)
            return {"status": "ok", "killed": target}

        return {"status": "error", "reason": f"unknown action: {action}"}


