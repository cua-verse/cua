# US-OC-033: Tool — Analyze Remote Image

## Problem

The agent saves milestone screenshots via `save_milestone_screenshot` but never sees the captured image. The tool returns a text-only success message (`"✅ Milestone screenshot saved to: ..."`), so the agent has no visual feedback to verify what was actually saved.

This caused systematic failures in Magic Tower runs (see `logs/loop_run/report.md`):
- **Off-by-one floor error**: Agent saved screenshots of wrong floors (Prologue instead of Floor 1, Floor 1 instead of Floor 2) and couldn't self-correct
- **Memory poisoning**: False success claims ("reached Floor 3") persisted across runs, making all retry attempts useless

## Solution

Add an `analyze_image` tool modeled after OpenClaw's `image` tool. Instead of returning raw image bytes for the main model to interpret (expensive — adds a full image to context), this tool sends the image to a VLM with a specific question and returns a **text analysis**. This is cheaper, more targeted, and avoids context bloat.

### Expected usage flow

```
1. Agent calls save_milestone_screenshot(path="...\2.png", description="Floor 2")
2. Gets back: {"success": true, "message": "✅ Milestone screenshot saved to: ...\2.png"}
3. Agent calls analyze_image(image="...\2.png", prompt="What floor number is shown in this game screenshot? Read the floor indicator text in the bottom-left panel.")
4. Gets back: "The floor indicator shows '序 章' (Prologue). This is the prologue area, not Floor 2."
5. Agent realizes the error, navigates further, and re-saves the milestone
```

### Why VLM analysis over raw image return

| Approach | Tokens | Context pressure | Accuracy |
|----------|--------|-----------------|----------|
| Return raw image | ~1500 tokens/image | High — each verification adds a full image to main context | Agent must interpret the image itself |
| VLM analysis (this approach) | ~100-200 tokens/response | Low — only text returned to main context | Dedicated VLM focuses on the specific question |

OpenClaw chose the VLM analysis approach for the same reasons. The main model already has screenshots flowing through the Computer tool — adding more images via tool results creates unnecessary context pressure.

## OpenClaw Reference

The OpenClaw source lives at `../openclaw/` (sibling directory to `agenthle-base/`).

**Primary reference**: `../openclaw/src/agents/tools/image-tool.ts`

OpenClaw's `image` tool:
- Accepts `image` (single path/URL) or `images` (multiple, up to 20) + `prompt`
- Loads image bytes from local files or URLs (`loadWebMedia`)
- Sends to a configurable VLM (`runImagePrompt`) with fallback chain (e.g., claude-opus → claude-sonnet)
- Returns text analysis result via `buildTextToolResult()`
- Handles model override per-call via `model` parameter
- Uses `runWithImageModelFallback()` for automatic retry on different models

**What we keep**:
- Core pattern: read image → send to VLM with prompt → return text analysis
- `prompt` parameter for targeted questions (default: "Describe the image.")
- Single `image` path parameter (simplify — drop multi-image `images` array for now)
- Configurable model (use `summary_model` from agent config as default VLM)

**What we drop**:
- Multi-image support (`images` array, `maxImages`) — not needed for milestone verification
- URL/web loading (`loadWebMedia`, `isHttpUrl`) — our images are always on the remote VM
- Sandbox/bridge filesystem abstractions — CUA uses `BaseComputerInterface` directly
- Model registry/auth discovery — we use litellm which handles this
- `maxBytesMb` size limiting — remote screenshots are bounded by VM resolution

**Key helpers to reference**:
- `image-tool.helpers.ts` — `decodeDataUrl`, `coerceImageModelConfig`, `resolveProviderVisionModelFromConfig`
- `media-tool-shared.ts` — `buildTextToolResult`, `resolvePromptAndModelOverride`

## Image Source: Remote VM vs Local

The agent interacts with two filesystems, and images can live on either:

| Source | Location | Path format | How to read |
|--------|----------|-------------|-------------|
| Milestone screenshots | Remote VM | `C:\Users\User\Desktop\game\output\2.png` | `interface.read_bytes(path)` |
| Turn screenshots (saved by TrajectorySaverCallback) | Local machine | `/Users/.../trycua/.../turn_000/0003_screenshot_after.png` | `open(path, "rb").read()` |
| Reference images | Local machine | `tasks/game/mota_24_easy/reference/1.png` | `open(path, "rb").read()` |

**The agent only knows about remote VM paths** (it chose them when calling `save_milestone_screenshot`). Turn screenshots are saved by the `TrajectorySaverCallback` to `trajectory_dir` but the agent never sees those local paths — screenshots flow to the agent as inline base64 via `computer_call_output`.

**Path detection heuristic**: Windows-style paths (`C:\...`, `D:\...`) → remote VM. Unix-style paths (`/Users/...`, `./...`) → local filesystem. This is the same pattern `MilestoneTool._is_windows_path()` already uses.

## Implementation

### Part 1: AnalyzeImageTool (new BaseTool)

**File**: `submodules/cua/libs/python/agent/agent/tools/analyze_image.py`

```python
@register_tool("analyze_image")
class AnalyzeImageTool(BaseTool):
    """Analyze an image from the remote computer or local filesystem using a vision model."""

    def __init__(self, interface: "BaseComputerInterface", model: str = None, cfg=None):
        self.interface = interface
        self.model = model or "anthropic/claude-sonnet-4-20250514"
        super().__init__(cfg)

    @property
    def description(self) -> str:
        return (
            "Analyze an image file by sending it to a vision model with a specific question. "
            "Returns a text description/analysis. The image can be on the remote computer "
            "(Windows path like C:\\...) or on the local machine (Unix path like /...). "
            "Use this to verify milestone screenshots, read game UI elements, or extract "
            "information from any image."
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "image": {
                    "type": "string",
                    "description": (
                        "Path to the image file. Use a Windows path (e.g., C:\\Users\\...) "
                        "for images on the remote VM, or a Unix path (e.g., /Users/...) "
                        "for images on the local machine."
                    ),
                },
                "prompt": {
                    "type": "string",
                    "description": "What to analyze in the image. Be specific about what information you need.",
                },
            },
            "required": ["image"],
        }

    def call(self, params, **kwargs):
        # 1. Parse params
        # 2. Detect path type:
        #    - Windows path (C:\...) → read from remote VM via self.interface.read_bytes(path)
        #    - Unix path (/...) → read from local filesystem via open(path, "rb").read()
        # 3. Base64-encode the image bytes
        # 4. Send to VLM via litellm.completion() with image + prompt
        # 5. Return text analysis result
        # On error: return {"success": False, "error": "..."}
        ...

    @staticmethod
    def _is_remote_path(path: str) -> bool:
        """Detect if path is a remote VM (Windows) path vs local (Unix) path."""
        import re
        return bool(re.match(r'^[A-Za-z]:\\', path) or path.startswith('\\\\') or '\\' in path)
```

Key implementation details:

**Dual-path image loading**:
```python
if self._is_remote_path(path):
    # Remote VM: read via interface
    image_bytes = await self.interface.read_bytes(path)
    # Fallback if read_bytes unavailable:
    # b64 = await self.interface.run_command(
    #     f"powershell [Convert]::ToBase64String([IO.File]::ReadAllBytes('{path}'))"
    # )
else:
    # Local filesystem: read directly
    with open(path, "rb") as f:
        image_bytes = f.read()
```

- Send to VLM via `litellm.completion()` (already used in compaction pipeline — see `context.py`)
- Default prompt: `"Describe the image."` (matches OpenClaw's `DEFAULT_PROMPT`)
- Model: use `summary_model` passed from agent config (cheap model for analysis, not the main agent model)
- Same async-in-sync execution pattern as MilestoneTool (`concurrent.futures.ThreadPoolExecutor`)
- Detect MIME type from file extension (`.png` → `image/png`, `.jpg` → `image/jpeg`, etc.)

### Part 2: Wire into build_tools()

**File**: `submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/tools.py`

```python
from agent.tools.analyze_image import AnalyzeImageTool

def build_tools(session, memory_store, summary_model=None):
    # ... existing tools ...
    analyze_image_tool = AnalyzeImageTool(session.interface, model=summary_model)
    return [session._computer, milestone_tool, analyze_image_tool, memory_search, memory_get, memory_write]
```

Note: `build_tools()` signature needs `summary_model` parameter added. Update the call in `openclaw_agent.py` to pass `self.summary_model`.

### Part 3: AGENTS.md guidance

**File**: `submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/AGENTS.md`

Add verification guidance to the milestone section:

```markdown
### Verifying Milestones

After saving a milestone screenshot, **always verify** it using `analyze_image`:
1. Call `save_milestone_screenshot(path="...", description="...")`
2. Call `analyze_image(image="<same path>", prompt="<specific verification question>")`
   - For game tasks: "What floor/level/stage is shown? Read any on-screen indicators."
   - For UI tasks: "What dialog/window/state is visible? Read any text labels."
3. If the analysis doesn't match your expectation, re-navigate and re-save before proceeding

**Do NOT trust your own reasoning about what the screenshot contains.** Always verify with `analyze_image`.
```

### Part 4: No agent loop changes needed

Unlike the raw-image-return approach, this tool returns **text** (the VLM's analysis), so no modifications to `agent.py`'s `function_call_output` construction are needed. The existing `str(result)` path works fine.

## Risks and edge cases

1. **VLM cost**: Each `analyze_image` call makes a separate API call. Use `summary_model` (cheaper model) to keep costs down. Monitor usage in long runs.
2. **VLM latency**: Adds ~2-5 seconds per call. Acceptable for milestone verification (infrequent), but don't use for rapid-fire analysis.
3. **`read_bytes` availability**: Verify `BaseComputerInterface` has `read_bytes(path)`. If not, use PowerShell base64 encoding via `run_command` as fallback.
4. **File not found**: Agent may call with a path that doesn't exist. Return clear text error, don't throw.
5. **Non-image files**: Agent might pass a non-image path. The VLM call will fail — catch and return a descriptive error.
6. **VLM hallucination**: The analysis VLM itself could hallucinate. This is lower risk than the main model hallucinating (the VLM is focused on one specific question), but not zero. For critical verifications, the agent could call `analyze_image` twice with different prompts.

## Relationship to US-OC-030

US-OC-030 ("Tool: Visual Analysis") described a similar concept but was scoped more broadly (region cropping, multi-image comparison, configurable VLM provider). This story (US-OC-033) is a focused implementation driven by the concrete milestone verification failure. US-OC-030 can be updated to reference this as a dependency or marked as superseded.
