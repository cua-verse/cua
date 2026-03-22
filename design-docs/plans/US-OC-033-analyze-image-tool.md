# US-OC-033: Tool — Analyze Remote Image (VLM-based)

## Context

The agent saves milestone screenshots via `save_milestone_screenshot` but has no way to verify what was captured without adding raw images to its context (~1500 tokens each). Logs show systematic milestone verification failures in Magic Tower. This tool lets the agent ask a VLM a specific question about image(s) and get a text answer (~100-200 tokens), keeping context lean.

Modeled after OpenClaw's `image-tool.ts` — reproducing multi-image and URL/data-URI support, simplified for litellm.

## OpenClaw Design Rationale

### What We Keep
- **Core pattern**: read image(s) → base64 → send to VLM with prompt → return text
- **Multi-image support**: `image` (single) + `images` (array, up to 20) — matches OpenClaw's `image-tool.ts:309-314`. Deduplication + max cap logic preserved.
- **URL/data-URI loading**: Support `http(s)://` URLs (fetched via httpx), `data:image/...;base64,...` URIs (decoded inline), and file paths. Matches OpenClaw's `image-tool.ts:399-422`.
- **Cheaper model for VLM calls**: OpenClaw uses `imageModel` config; we use `summary_model` (already wired for compaction).
- **Text-only return**: No images in tool result — context efficiency.
- **Default prompt**: "Describe the image." when prompt not provided (OpenClaw `image-tool.ts:36`).
- **`maxBytesMb` safety cap**: Optional parameter to limit image size before sending to VLM (OpenClaw `image-tool.ts:160-169`). Default 10MB. Rejects images exceeding the limit with an error message — prevents accidental cost spikes from huge screenshots or downloaded images.

### What We Drop
- **Provider fallback chains**: OpenClaw has primary + fallback models with MiniMax/OpenAI/Anthropic-specific logic. litellm handles provider abstraction for us.
- **Sandbox filesystem**: OpenClaw has sandboxed path resolution (`SandboxFsBridge`, `resolveSandboxedBridgeMediaPath`). Not applicable to CUA.
- **Model registry discovery**: OpenClaw resolves models from JSON registry. We just pass a model string to litellm.
- **`file://` URL scheme**: Unnecessary complexity — local paths work directly.

### Key Differences from OpenClaw
- CUA has a remote VM — dual-path loading: Windows paths (`C:\...`) → `interface.read_bytes()`, Unix paths → local `open()`
- litellm replaces OpenClaw's `complete()` from pi-ai
- `summary_model` replaces OpenClaw's `imageModel` config
- HTTP fetching uses `httpx` (already in deps) instead of OpenClaw's `loadWebMedia`

## File Changes

### 1. NEW: `submodules/cua/libs/python/agent/agent/tools/analyze_image.py`

Core tool implementation following MilestoneTool patterns:

- `@register_tool("analyze_image")` class `AnalyzeImageTool(BaseTool)`
- Constructor: `__init__(self, interface, model=None, cfg=None)` — stores interface + model string

**Parameters schema** (matches OpenClaw's tool parameters):
- `image` (optional, string) — single image: file path, HTTP URL, or data URI
- `images` (optional, array of strings) — multiple images (up to 20)
- `prompt` (optional, string) — question about the image(s), default "Describe the image."
- `maxBytesMb` (optional, number) — max size per image in MB, default 10. Images exceeding this are rejected.
- At least one of `image` or `images` must be provided

**Input normalization** (from OpenClaw `image-tool.ts:325-345`):
- Merge `image` + `images` into a single list, dedup while preserving order
- Strip leading `@` prefix (OpenClaw convention)
- Enforce max 20 images cap; return error text if exceeded

**Image loading** — `_load_image(raw_input)` resolves each input to `(base64, mime_type)`:

| Input type | Detection | Loading |
|---|---|---|
| Data URI | `^data:image/...;base64,...` | Decode inline (like OpenClaw `decodeDataUrl`) |
| HTTP(S) URL | `^https?://` | `httpx.AsyncClient.get(url)` → bytes |
| Windows path | `^[A-Za-z]:\\` or contains `\\` | `interface.read_bytes(path)` |
| Unix path | Everything else | `open(path, 'rb').read()` |
| Unsupported scheme | `^[a-z]+:` but not above | Return error text (OpenClaw `image-tool.ts:409-422`) |

**Size enforcement**: After loading bytes (from any source), check `len(bytes) > maxBytesMb * 1024 * 1024`. If exceeded, return error: `"Error: image too large ({actual_mb:.1f} MB), maximum is {maxBytesMb} MB: {input}"`.

**MIME detection**:
- Data URIs: extracted from the URI itself
- HTTP responses: from `Content-Type` header, fallback to extension
- File paths: from extension (`.png`, `.jpg`, `.jpeg`, `.gif`, `.webp`, `.bmp`; default `image/png`)

**VLM call** — `_execute_analyze(images, prompt)`:
1. Build litellm messages with all loaded images:
   ```python
   content = [{"type": "text", "text": prompt}]
   for b64, mime in images:
       content.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})
   messages = [{"role": "user", "content": content}]
   ```
2. `litellm.acompletion(model=self.model, messages=messages, max_tokens=1024, temperature=0.3, timeout=60)`
3. Return `response.choices[0].message.content`

**Sync wrapper**: `call()` uses ThreadPoolExecutor + `asyncio.run()` (same pattern as milestone.py:77-90)

**Error handling** — all errors return descriptive text strings:
- No images provided: `"Error: at least one image is required (use 'image' or 'images' parameter)."`
- Too many images: `"Error: too many images: N provided, maximum is 20."`
- Unsupported scheme: `"Error: unsupported image reference: {input}. Use a file path, data: URL, or http(s) URL."`
- Data URI decode failure: `"Error: invalid data URL — {details}"`
- File not found (local): `"Error: file not found: {path}"`
- Remote read failure: `"Error: could not read file from remote VM: {path} — {error}"`
- HTTP fetch failure: `"Error: failed to fetch image from URL: {url} — {error}"`
- Empty file: `"Error: file is empty: {path}"`
- Non-image MIME in data URI: `"Error: unsupported data URL type: {mime}"`
- VLM call failure: `"Error: image analysis failed — {error}"`

### 2. MODIFY: `submodules/cua/libs/python/agent/agent/tools/__init__.py`

Add import and export:
```python
from .analyze_image import AnalyzeImageTool
# Add "AnalyzeImageTool" to __all__
```

### 3. MODIFY: `submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/tools.py`

- Add `summary_model: str | None = None` parameter to `build_tools()`
- Import `AnalyzeImageTool` from `agent.tools`
- Create `AnalyzeImageTool(session.interface, model=summary_model)` and add to returned list

### 4. MODIFY: `submodules/cua/libs/cua-bench/cua_bench/agents/openclaw_agent.py`

One-line change — pass `summary_model` to `build_tools()`:
```python
tools = build_tools(session, memory_store, summary_model=self.summary_model)
```

### 5. MODIFY: `submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/AGENTS.md`

Add verification guidance after the Milestones section:
- After saving a milestone, always verify with `analyze_image(image="<same path>", prompt="<verification question>")`
- Multi-image comparison: `analyze_image(images=["path1.png", "path2.png"], prompt="Compare these two screenshots...")`
- Specific prompt suggestions for game vs UI tasks
- "Do NOT trust your own reasoning about what the screenshot contains"

### 6. NEW: `tests/test_analyze_image_tool.py`

Unit tests (mock interface + litellm + httpx, no real API calls):
- Tool registration in TOOL_REGISTRY
- Schema: `image` optional, `images` optional, `prompt` optional
- Input normalization: dedup, `@` stripping, max cap
- Path detection: Windows → remote, Unix → local
- Remote read via mocked `interface.read_bytes()`
- Local read via `tmp_path` fixture
- Data URI decoding (valid + invalid)
- HTTP URL loading (mocked httpx)
- Unsupported scheme detection
- Multi-image: multiple images forwarded to VLM
- No images provided → error string
- Too many images → error string
- File not found → error string
- VLM failure → error string
- Default prompt used when omitted
- Model forwarded to litellm

## Implementation Sequence

1. Create `analyze_image.py`
2. Update `agent/tools/__init__.py`
3. Create `tests/test_analyze_image_tool.py` + verify tests pass
4. Update `openclaw/tools.py` — wire into `build_tools()`
5. Update `openclaw_agent.py` — pass `self.summary_model`
6. Update `AGENTS.md`
7. Lint: `uv run ruff check .`
8. Full tests: `uv run pytest tests/`

## Verification

- **Level 1**: `uv run ruff check .` + `uv run pytest tests/test_analyze_image_tool.py`
- **Level 2**: `run_magic_tower.sh 50` — confirm agent calls `analyze_image` after milestones, trajectory shows text analysis in tool result
