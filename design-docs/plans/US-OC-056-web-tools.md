# US-OC-056 — Web Search + Web Fetch Tools

## Context

The main agent currently cannot reach the internet. Tasks that would benefit from external context — docs lookups, error-message explanations, walkthroughs — either stall or force the user to paste content into the task description. US-OC-031's tool-migration audit flagged **web_search + web_fetch** as the single highest-ROI pure-BaseTool migration (Category 5, P1, low complexity), with no dependency blockers. This story adds both tools as BaseTool subclasses in a new `tools_web.py` module, wires them into `build_tools()`, and validates end-to-end against a live VM run.

Decisions from clarification:
- **Search provider**: Brave Search API (`https://api.search.brave.com/res/v1/web/search`). Key from `BRAVE_API_KEY` env var. Matches OpenClaw's primary provider.
- **HTML extractor**: `readability-lxml` (new dep). Direct Mozilla Readability port; closest Python analogue to OpenClaw's `@mozilla/readability`.

---

## OpenClaw Design Rationale

### What OpenClaw Does

OpenClaw ships two agent-facing web tools in `openclaw/src/agents/tools/`:

- **`web-search.ts`** (~60 lines) — thin wrapper over `runWebSearch()` in `openclaw/src/web-search/runtime.ts`. Resolves a provider (Brave / Perplexity / Grok / Kimi) via a manifest plugin registry and runtime credential scoping, caches via `SEARCH_CACHE`, returns provider-normalized results.
- **`web-fetch.ts`** (~630 lines) — full pipeline: parse URL → SSRF guard (`web-guarded-fetch.ts` → `infra/net/ssrf.ts`) → `fetch()` with redirect + timeout + max-bytes limits → content-type-specific extractor (Cloudflare markdown header / Readability on HTML / pretty-print JSON) → `wrapWebContent()` untrusted-content wrapper → `truncateText()` to `maxChars` → optional provider-fallback on failure. Cache via `FETCH_CACHE`.

System prompt (`system-prompt.ts:476-477`) ships only **one-line descriptions** for both tools — no dedicated operational prose block.

### What We Keep and Why

| Kept | Why |
|---|---|
| **Single Brave provider** for search | Faithful to OpenClaw's primary provider; PRD accepts "pick one provider to ship with." |
| **SSRF guard for web_fetch** | Explicit PRD criterion. Loopback / private / link-local / metadata-service IPs must be rejected before any TCP connect. |
| **`http`/`https` scheme allow-list** | Matches OpenClaw `web-fetch.ts:381-383`. |
| **Timeout + max-redirect + max-response-bytes caps** | Matches OpenClaw `DEFAULT_FETCH_MAX_REDIRECTS=3`, `DEFAULT_TIMEOUT_SECONDS` via aiohttp `ClientTimeout`, `DEFAULT_FETCH_MAX_RESPONSE_BYTES=750_000`. |
| **`maxChars` default 20_000 + truncation marker** | Matches OpenClaw `DEFAULT_FETCH_MAX_CHARS`. Prevents the agent from dumping a 500 KB article into context. |
| **Readability-based extraction** | Matches OpenClaw `extractReadableContent` (`web-fetch-utils.ts`). |
| **Basic-HTML fallback when readability returns empty** | Matches OpenClaw `extractBasicHtmlContent`. Readability often returns empty on docs landing / search-result / API-reference pages. Use bs4+html5lib (already core deps): strip `<script>/<style>/<nav>/<footer>/<aside>/<header>`, `get_text(separator="\n")`, collapse blank lines. |
| **`htmlToMarkdown` conversion** | OpenClaw `htmlToMarkdown`. Used (a) for `extractMode="markdown"` path (default), (b) to render HTML error-page bodies into readable error detail. Add `markdownify>=0.11.6` dep (pure Python, small). |
| **`extractMode: "markdown" \| "text"` param** | OpenClaw default. Markdown preserves doc structure (headings, lists, links) that LLMs use well. Default to `"markdown"`; `"text"` remains available. |
| **Per-process `FETCH_CACHE` + `SEARCH_CACHE` with TTL** | Matches OpenClaw `web-shared.ts`. In-memory dict keyed by `(query,count,freshness,country)` for search, `(url,extractMode,maxChars)` for fetch. TTL: search 5 min, fetch 10 min. Saves Brave quota + latency on repeats within a run. |
| **Brave filters: `freshness`, `country`, `date_after`** | Optional tool params. `freshness` = `pd\|pw\|pm\|py` natively; `country` = ISO code; `date_after=YYYY-MM-DD` mapped to Brave's range syntax `freshness=YYYY-MM-DDto<today>` (see `web-search-provider-common.ts:261`). |
| **Schema-only prompt surface** | OpenClaw `system-prompt.ts:476-477` ships one-liners; our audit meta-finding (progress.txt Codebase Patterns) says schema-only is the faithful port. **No `_build_web_search()` / `_build_web_fetch()` added to `prompt.py`.** |
| **AGENTS.md unchanged** | Tool-name-free rule (US-OC-068). No prose added. |
| **`BaseTool.description` is a one-liner** | Matches OpenClaw's one-line entries — not multi-paragraph. |

### What We Drop and Why

| Dropped | Why |
|---|---|
| Multi-provider framework (Brave + Perplexity + Grok + Kimi) | We run in a benchmarking context with a single provider pin; the plugin registry is overkill. |
| Runtime credential scoping / manifest plugin system | No plugin system in harness. Plain env var is sufficient. |
| `resolveManifestContractOwnerPluginId`, `resolveWebSearchDefinition`, `resolveWebSearchProviderId` | Consequence of dropping multi-provider. |
| `wrapWebContent()` untrusted-content wrapper | OpenClaw wraps external content with an injection-warning preamble because its agent is multi-tenant and exposed to hostile inputs. Our agent runs benchmark tasks; attack surface is lower. **Revisit if we ever host untrusted tasks.** |
| Cloudflare Markdown-for-Agents header branch | Niche CDN optimization. |
| Provider-fallback on extraction failure | Dropped along with multi-provider. Fall through to basic-HTML fallback. |

### Key Differences from OpenClaw

| Area | OpenClaw | Ours | Reason |
|---|---|---|---|
| **HTTP client** | Native `fetch()` (Node 20+) | `aiohttp.ClientSession` | Already the harness-wide async HTTP pattern (`cloud.py:57-68`). No need to add httpx. |
| **SSRF implementation** | `infra/net/ssrf.ts` — resolves DNS, inspects each IP, rejects policy violations | Python `ipaddress.ip_address(gethostbyname(host))` — resolve at URL-parse time, reject if `is_private` / `is_loopback` / `is_link_local` / `is_multicast` / `is_reserved` / `is_unspecified`. Also reject bare-IP URLs matching those classes. | Same behavior, simpler pipeline. No Happy Eyeballs nuances because we only need one address class. |
| **Config surface** | Nested `config.tools.web.{search,fetch}` with per-field resolvers | Two module constants + one env var per tool | Single provider; no user-facing config system in harness. |
| **Error on missing key** | Tool returns `null` at registration time | Tool registers unconditionally; raises `ValueError` at `call()` time with actionable message | Unit tests stay simple; `build_tools()` stays uniform. Matches PRD criterion "absence of config raises a clear error, not a silent failure." |
| **Sync→async inside `BaseTool.call`** | N/A (TypeScript is naturally async) | Use existing `_run_async` helper from `tools_fs.py` — already-proven pattern for remote-VM tools | Zero new infra. |

---

## Files to Create / Modify

### New

- `submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/tools_web.py` — both tools + SSRF guard + Brave client.
- `tests/test_web_tools.py` — unit tests (tool-registration, param validation, SSRF positive/negative, Brave client mocked, fetch happy/truncation/error paths, readability integration).
- `smoke/web_tools_vm.py` — Level 2 smoke script (no VM needed: HTTP-only). Exercises both tools against live endpoints.
- `docs/plan/US-OC-056-web-tools.md` — copy of this plan (persisted by onboard step 9).

### Modify

- `submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/tools.py` — import `WebSearchTool, WebFetchTool`; instantiate in `build_tools()` between `exec_tool` and memory tools; append to `tools` list.
- `submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/__init__.py` — re-export `WebSearchTool, WebFetchTool`.
- `submodules/cua/libs/cua-bench/pyproject.toml` — add `readability-lxml>=0.8.1`, `lxml[html_clean]>=5.0`, and `markdownify>=0.11.6` to `dependencies`.
- `tests/test_openclaw_tools.py` — bump tool-count assertion 10 → 12.
- `prd.json` — set `context.planFile` for US-OC-056 to `docs/plan/US-OC-056-web-tools.md` (via `/prd`).

---

## Implementation Sketch

### `WebSearchTool`

```python
@register_tool("web_search")
class WebSearchTool(BaseTool):
    description = "Search the web (Brave API). Returns ranked results with title/url/snippet."

    params_schema = {
        "type": "object",
        "properties": {
            "query":      {"type": "string",  "description": "Search query."},
            "count":      {"type": "integer", "description": "Results to return (1-20, default 5).", "minimum": 1, "maximum": 20},
            "freshness":  {"type": "string",  "description": "Time filter: 'pd' (day) | 'pw' (week) | 'pm' (month) | 'py' (year)."},
            "country":    {"type": "string",  "description": "ISO country code, e.g. 'US', 'JP'. Biases results to that locale."},
            "date_after": {"type": "string",  "description": "Only results newer than YYYY-MM-DD. Mapped to Brave's range freshness syntax."},
        },
        "required": ["query"],
    }

    def __init__(self, *, api_key: str | None = None, cfg=None):
        super().__init__(cfg)
        # Resolve API key at first call to avoid construction-time errors in tests.
        self._api_key_explicit = api_key

    def call(self, args):
        # _verify_json_format_args; resolve query/count/freshness/country/date_after
        api_key = self._api_key_explicit or os.environ.get("BRAVE_API_KEY")
        if not api_key:
            raise ToolError("web_search requires BRAVE_API_KEY (env var) or an api_key kwarg.")
        # If date_after set and freshness absent, map: freshness = f"{date_after}to{today}"
        cache_key = (query, count, freshness, country)
        hit = _SEARCH_CACHE.get(cache_key)                      # TTL-aware getter
        if hit is not None:
            return {**hit, "cached": True}
        result = _run_async(self._search(api_key, query, count, freshness, country))
        _SEARCH_CACHE.set(cache_key, result, ttl_seconds=300)
        return result

    async def _search(self, api_key, query, count, freshness, country):
        # aiohttp.ClientSession, headers={"X-Subscription-Token": api_key, "Accept": "application/json"}
        # GET https://api.search.brave.com/res/v1/web/search?q=...&count=...[&freshness=...][&country=...]
        # ClientTimeout(total=10). Map 429 -> ToolError("rate limited; retry after ...s") using Retry-After header.
        # Parse .web.results[] -> [{title, url, description}]; return {"provider": "brave", "results": [...]}
```

### `WebFetchTool`

```python
@register_tool("web_fetch")
class WebFetchTool(BaseTool):
    description = "Fetch and extract readable text from an HTTP(S) URL. Use for lightweight page access without browser automation."

    params_schema = {
        "type": "object",
        "properties": {
            "url":         {"type": "string",  "description": "HTTP or HTTPS URL."},
            "extractMode": {"type": "string",  "enum": ["markdown", "text"], "description": "Output format (default 'markdown')."},
            "maxChars":    {"type": "integer", "description": "Character cap on returned text (default 20000, max 100000).", "minimum": 100},
        },
        "required": ["url"],
    }

    def call(self, args):
        # validate url scheme; _assert_url_safe(url)  # SSRF guard
        extract_mode = args.get("extractMode", "markdown")
        cache_key = (url, extract_mode, max_chars)
        hit = _FETCH_CACHE.get(cache_key)
        if hit is not None:
            return {**hit, "cached": True}
        result = _run_async(self._fetch(url, extract_mode, max_chars))
        _FETCH_CACHE.set(cache_key, result, ttl_seconds=600)
        return result

    async def _fetch(self, url, extract_mode, max_chars):
        # aiohttp.ClientSession, User-Agent=OpenClaw's DEFAULT_FETCH_USER_AGENT, ClientTimeout(total=30), max_redirects=3.
        # Stream iter_chunked, abort after 750_000 bytes (matches OpenClaw DEFAULT_FETCH_MAX_RESPONSE_BYTES).
        # Route on content-type:
        #   text/html:
        #     1. readability-lxml -> if non-empty text: text = (html->markdown via markdownify) if extract_mode=="markdown" else plain text
        #     2. else basic-HTML fallback (bs4: drop script/style/nav/footer/aside/header, get_text, collapse blank lines)
        #     3. else raise ToolError("extraction failed")
        #   application/json:    pretty-printed JSON
        #   text/markdown:       body as-is (convert to text if extract_mode=="text")
        #   text/plain | other:  body as-is
        #   non-2xx HTML:        htmlToMarkdown(body) then truncate -> included in ToolError detail
        # Truncate text to max_chars with "... [truncated N chars]" marker.
        # Return {"url", "finalUrl", "status", "contentType", "title", "extractMode",
        #         "extractor": "readability" | "basic-html" | "json" | "raw",
        #         "text", "truncated": bool, "length": int, "fetchedAt": iso8601}
```

### SSRF guard

```python
_BLOCKED_IP_PREDICATES = (
    "is_private", "is_loopback", "is_link_local",
    "is_multicast", "is_reserved", "is_unspecified",
)

def _assert_url_safe(url: str) -> None:
    try:
        parsed = urlparse(url)
    except ValueError:
        raise ToolError(f"Invalid URL: {url!r}")
    if parsed.scheme not in ("http", "https"):
        raise ToolError(f"Only http/https URLs are allowed (got {parsed.scheme!r}).")
    host = parsed.hostname
    if not host:
        raise ToolError("URL is missing a host.")
    # Resolve via getaddrinfo — catches CNAME-to-private and DNS-rebinding-at-parse-time
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        raise ToolError(f"DNS resolution failed for {host!r}: {e}")
    for family, _, _, _, sockaddr in infos:
        ip_str = sockaddr[0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            raise ToolError(f"Could not parse resolved IP {ip_str!r} for {host!r}.")
        for pred in _BLOCKED_IP_PREDICATES:
            if getattr(ip, pred, False):
                raise ToolError(f"URL {url!r} resolves to blocked address {ip_str} ({pred}).")
```

**Known limitation:** DNS-rebinding between check and `aiohttp.get()` — the fetch may resolve to a different IP. OpenClaw's `fetchWithSsrFGuard` pins the resolved address into the socket via a custom `LookupFn`. For v1 we accept the race; it's a benchmark harness, not a multi-tenant service. Follow-up story if the harness ever ingests untrusted task authors.

### `build_tools()` integration

Insert between `exec_tool` and `memory_search`:

```python
exec_tool = ExecTool(session.interface, workspace_root=workspace_root)
web_search = WebSearchTool()            # api_key resolved lazily at call
web_fetch = WebFetchTool()
memory_search = MemorySearchTool(memory_store)
# ...
tools: list = [
    computer, milestone_tool, analyze_image_tool,
    read_tool, write_tool, edit_tool, exec_tool,
    web_search, web_fetch,
    memory_search, memory_get, memory_write,
]
```

No new kwargs to `build_tools()`. No AGENTS.md changes. No `prompt.py` changes.

---

## Acceptance Criteria Coverage

| PRD clause | Covered by |
|---|---|
| L1 lint (`uv run ruff check .`) | Ensured before commit. |
| L1 WebSearchTool with mocked provider returning ranked results | `test_web_tools.py::TestWebSearch` — patches `aiohttp.ClientSession.get`, asserts ranked list shape. |
| L1 WebFetchTool fetches URL, extracts via readability, respects maxChars | `test_web_tools.py::TestWebFetch` — mocked responses (HTML, JSON, plain), readability→markdownify path covered for `extractMode="markdown"` (default) and `"text"`, basic-HTML fallback triggered when readability returns empty, HTML error body rendered via htmlToMarkdown, truncation marker verified, fetch cache hit returns `cached=True`. |
| L1 WebSearchTool filter params | `TestWebSearch::test_date_after_maps_to_freshness_range` + `test_country_forwarded` + `test_freshness_pd_forwarded`; `test_cache_hit_returns_cached_true`. |
| L1 SSRF guard — 127.0.0.1, 169.254.169.254, 10.0.0.0/8, and valid public URLs | `test_web_tools.py::TestSsrfGuard` — `_assert_url_safe` parametrized: `http://127.0.0.1/x` → rejected (loopback), `http://169.254.169.254/` → rejected (link-local, covers metadata service), `http://10.1.2.3/x` → rejected (private), `http://example.com/x` → allowed. Also `ftp://...` rejected (scheme), `http://[::1]/` rejected (IPv6 loopback). |
| L1 Provider + API key configurable; absence raises clear error | Constructor accepts `api_key=None`; first `call()` without env var raises `ToolError("web_search requires BRAVE_API_KEY ...")`. Test: `test_missing_api_key_raises`. |
| L2 trajectory shows at least one organic invocation | Run `bash run_magic_tower.sh 50` after adding a non-GUI nudge (following the P2 pattern in progress.txt: one step in `task.md` phrased as "look up official guidance on X and cite it" — agent infers `web_search` / `web_fetch`). Verify turn_NNN/NNNN_agent_response.json contains a `function_call` with `name: "web_search"` or `"web_fetch"`. |

---

## Verification

### Level 1 (local, no VM)

```bash
cd /media/volume/MOL-System/agenthle-base
uv run ruff check submodules/cua/libs/cua-bench/cua_bench/agents/openclaw/tools_web.py tests/test_web_tools.py
uv run pytest tests/test_web_tools.py -v
uv run pytest tests/ -q    # full suite, expect prior 2 unrelated failures to persist
```

### Level 2 (VM)

1. Add a non-GUI nudge to `tasks/game/mota_24_easy/task.md` (e.g. step 0: "look up basic strategy guidance for Magic Tower 24 and cite one tip that applies here").
2. `BRAVE_API_KEY=<key> bash run_magic_tower.sh 50 > logs/us_oc_056/run_$(date +%Y%m%d_%H%M%S).log 2>&1`
3. `grep -E '"name": "web_(search|fetch)"' logs/us_oc_056/run_*.log` must find at least one hit.
4. Inspect one full turn (`trycua/.../turn_NNN/*_agent_response.json`) to confirm the tool returned structured data, not an error.

### Smoke (fallback if Brave key unavailable)

`smoke/web_tools_vm.py` — standalone HTTP test: fetch `https://example.com`, fetch `https://httpbin.org/json`, fetch a known-good HTML article with readability, assert SSRF guard blocks `http://127.0.0.1/` and `http://169.254.169.254/`. Does not require the remote VM — only outbound HTTP from the harness host.

---

## Open Risks

1. **`readability-lxml` install footprint** — brings `lxml` (C extension). Already common in Python data stacks; should install cleanly via uv on Ubuntu. Fallback plan if install fails: the basic-HTML fallback (bs4 + html5lib, already core deps) is sufficient on its own; drop readability-lxml + markdownify and ship text-only extraction. Quality drop but keeps the tool usable.
2. **DNS-rebinding race between SSRF check and aiohttp fetch** — acknowledged above; deferred.
3. **Brave free tier (2k/mo)** — fine for dev; heavy benchmark runs may hit the limit. Add error handling for 429 responses that surfaces rate-limit info to the agent.
4. **`task.md` nudge** — if adding a web-search step to the magic-tower task feels forced, we can instead run the L2 check against a new tiny task (`tasks/research/<name>`). Prefer the nudge for speed.
