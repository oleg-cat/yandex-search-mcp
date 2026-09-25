# yandex-search-mcp

[По-русски → README.ru.md](README.ru.md)

Self-hosted MCP server for **Yandex Search API v2**: web search, image search, and generative search (AI answer with cited sources). Built for Russian-language search (all 6 Yandex indexes: ru/com/tr/kk/be/uz), STDIO or Streamable HTTP transport, fully typed tool parameters, structured output.

Works with **Claude Code**, **Codex CLI**, and **opencode** (any MCP client with stdio support).

## Why

The official `yandex/yandex-search-mcp-server` is a Turkish-market demo: only tr/en regions, XML parsed with regexes, a non-existent dependency pin, and a `json.loads(resp[1:-1])` hack on generative search. This server is a from-scratch replacement modeled on the structure and quality of `brave/brave-search-mcp-server`:

- proper XML parsing with `defusedxml` (untrusted web content), parser written against **live API fixtures**;
- typed parameters with fail-fast validation (no `body: dict`);
- retries with exponential backoff on 429/5xx/network only; a unified JSON error contract;
- the API key never leaks into logs or error messages (covered by tests);
- image results contain URLs and metadata only — never base64 (a lesson from Brave's 2.0 breaking change);
- LLM-facing tool descriptions with "when to use / when NOT to use" guidance.

## Tools

| Tool | What it does | When to use |
|---|---|---|
| `yandex_web_search` | Classic web search: ranked documents (url, title, snippet) | The default: facts, news, research |
| `yandex_image_search` | Image search by text query: URLs and metadata | Pictures, diagrams, references |
| `yandex_gen_search` | One AI-synthesized answer with cited sources | Expensive/slow; only when a digest is explicitly needed |

## Getting credentials

1. [Create an API key](https://yandex.cloud/en/docs/iam/operations/authentication/manage-api-keys) for a service account with scope `yc.search-api.execute`.
2. Grant the service account the **`search-api.editor`** role on the folder.
3. Get your **Folder ID** ([how to find it](https://yandex.cloud/en/docs/resource-manager/operations/folder/get-id)).

API docs: [Search API v2](https://yandex.cloud/en/docs/search-api/) · [REST reference](https://aistudio.yandex.ru/docs/en/search-api/api-ref/).

## Installation

Requires Python ≥ 3.11.

**Fastest — no clone, via [uv](https://docs.astral.sh/uv/):**

```bash
YANDEX_SEARCH_API_KEY=<key> YANDEX_FOLDER_ID=<folder> uvx --from git+https://github.com/oleg-cat/yandex-search-mcp yandex-search-mcp
```

With a client: `claude mcp add yandex-search -e YANDEX_SEARCH_API_KEY=<key> -e YANDEX_FOLDER_ID=<folder> -- uvx --from git+https://github.com/oleg-cat/yandex-search-mcp yandex-search-mcp`.

**From source:**

```bash
git clone https://github.com/oleg-cat/yandex-search-mcp.git
cd yandex-search-mcp
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install -e .
```

Quick check (secrets go through env only — CLI arguments are visible in `ps`):

```bash
YANDEX_SEARCH_API_KEY=<key> YANDEX_FOLDER_ID=<folder> .venv/bin/python -m yandex_search_mcp
# server listens on STDIO; Ctrl+C to exit
```

## Claude Code

```bash
claude mcp add yandex-search \
  -e YANDEX_SEARCH_API_KEY=<key> \
  -e YANDEX_FOLDER_ID=<folder> \
  -- /abs/path/to/yandex-search-mcp/.venv/bin/python -m yandex_search_mcp
```

Use the **absolute path** to the venv python. Verify with `claude mcp list` (should show "✔ Connected").

## Codex CLI

```bash
codex mcp add yandex-search \
  --env YANDEX_SEARCH_API_KEY=<key> \
  --env YANDEX_FOLDER_ID=<folder> \
  -- /abs/path/to/yandex-search-mcp/.venv/bin/python -m yandex_search_mcp
```

Or manually in `~/.codex/config.toml`:

```toml
[mcp_servers.yandex-search]
command = "/abs/path/to/yandex-search-mcp/.venv/bin/python"
args = ["-m", "yandex_search_mcp"]
tool_timeout_sec = 180  # default 60s is too low for yandex_gen_search

[mcp_servers.yandex-search.env]
YANDEX_SEARCH_API_KEY = "<key>"
YANDEX_FOLDER_ID = "<folder>"
```

**Note:** Codex's default `tool_timeout_sec` is 60 seconds; `yandex_gen_search` can take tens of seconds — raise it to 180. Check the connection with `/mcp` inside the Codex TUI.

## opencode

`opencode.json` in your project root (secrets via `{file:...}` or `{env:...}`, not inline):

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "yandex-search": {
      "type": "local",
      "command": ["/abs/path/to/yandex-search-mcp/.venv/bin/python", "-m", "yandex_search_mcp"],
      "environment": {
        "YANDEX_SEARCH_API_KEY": "{file:~/.secrets/yandex_search_api_key}",
        "YANDEX_FOLDER_ID": "{file:~/.secrets/yandex_folder_id}"
      }
    }
  }
}
```

## Docker

```bash
docker build -t yandex-search-mcp .
docker run -i --rm \
  -e YANDEX_SEARCH_API_KEY=<key> \
  -e YANDEX_FOLDER_ID=<folder> \
  yandex-search-mcp
```

The container speaks STDIO by default (`-i` is required). For a long-running shared server use HTTP mode:

```bash
docker run --rm -p 8000:8000 \
  -e YANDEX_MCP_TRANSPORT=http -e YANDEX_MCP_HOST=0.0.0.0 \
  -e YANDEX_SEARCH_API_KEY_FILE=/run/secrets/yandex_key -v ./yandex_key:/run/secrets/yandex_key:ro \
  -e YANDEX_FOLDER_ID=<folder> \
  yandex-search-mcp
# MCP endpoint: http://localhost:8000/mcp
```

## HTTP transport

`YANDEX_MCP_TRANSPORT=http` serves MCP Streamable HTTP at `http://<host>:<port>/mcp` (default `127.0.0.1:8000`). On localhost DNS-rebinding protection is on automatically. The endpoint has **no authentication** — bind to `0.0.0.0` only behind a reverse proxy or inside a private network, since anyone who reaches it spends your Yandex quota.

## Environment variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `YANDEX_SEARCH_API_KEY` | yes* | — | Api-Key (scope `yc.search-api.execute`) |
| `YANDEX_SEARCH_API_KEY_FILE` | no | — | *Alternative: path to a file with the key (Docker/K8s secrets) |
| `YANDEX_FOLDER_ID` | yes | — | Folder ID (role `search-api.editor`) |
| `YANDEX_MCP_ENABLED_TOOLS` | no | all | Tool whitelist, space- or comma-separated, e.g. `"yandex_web_search"` |
| `YANDEX_MCP_DISABLED_TOOLS` | no | — | Tool blacklist, applied after the whitelist, e.g. `"yandex_gen_search"` |
| `YANDEX_MCP_DEFAULT_SEARCH_TYPE` | no | `ru` | Default index: `ru/com/tr/kk/be/uz` |
| `YANDEX_MCP_DEFAULT_REGION` | no | — | Default geo-id (225 = Russia, 213 = Moscow) |
| `YANDEX_MCP_TIMEOUT_WEB` | no | `15` | Web/image request timeout, seconds |
| `YANDEX_MCP_TIMEOUT_GEN` | no | `120` | Gen request timeout, seconds |
| `YANDEX_MCP_LOG_LEVEL` | no | `INFO` | Log level (logs go to stderr only) |
| `YANDEX_MCP_TRANSPORT` | no | `stdio` | `stdio` or `http` (Streamable HTTP) |
| `YANDEX_MCP_HOST` / `YANDEX_MCP_PORT` | no | `127.0.0.1` / `8000` | HTTP bind address |

## Tool parameters

### `yandex_web_search`

| Parameter | Type | Default | Description |
|---|---|---|---|
| `query` | str, 1–400 | — | Supports Yandex operators: `site:`, `host:`, `date:`, `"exact phrase"`, `-minus-word`, `\|` |
| `search_type` | `ru/com/tr/kk/be/uz` | from env | Search index |
| `n_results` | int, 1–20 | 10 | 5 for quick fact checks, 15–20 for research |
| `page` | int ≥ 0 | 0 | Pagination (follow `has_more`) |
| `region` | int | from env | Geo-id affecting ranking: 225 Russia, 213 Moscow, 2 St. Petersburg |
| `localization` | `ru/uk/be/kk/tr/en` | = search_type | Search UI language |
| `period` | `all/day/2weeks/month` | all | Document freshness |
| `sort_by` | `relevance/time` | relevance | `time` + `period` for news |
| `family_mode` | `none/moderate/strict` | moderate | Adult-content filtering |
| `fix_typos` | bool | true | Auto-correct query typos |
| `max_passages` | int, 1–5 | 3 | Snippet passages per result |
| `dedupe_by_domain` | bool | false | At most one result per domain |

Returns: `{query, corrected_query, found, page, has_more, results[{rank, url, domain, title, snippet, modified_at}]}`.

### `yandex_image_search`

| Parameter | Type | Default |
|---|---|---|
| `query`, `search_type`, `n_results`, `page`, `family_mode` | as above | — |
| `image_format` | `jpeg/gif/png` | — |
| `image_size` | `enormous/large/medium/small/tiny/wallpaper` | — |
| `orientation` | `horizontal/vertical/square` | — |
| `color` | `color/grayscale/red/…/black` | — |
| `site` | str | — |

Returns: `{query, found, page, has_more, results[{rank, image_url, format, width, height, page_url, domain}]}` — URLs and metadata only, **no base64**.

### `yandex_gen_search`

| Parameter | Type | Description |
|---|---|---|
| `query` | str | The question |
| `search_type` | as above | Index |
| `site` / `host` | str or list, ≤ 5 | Restrict sources to sites (with subdomains) / exact hosts |
| `url` | str or list, ≤ 10 | Restrict sources to exact pages. `site`, `host`, `url` are mutually exclusive |
| `fix_typos` | bool, default true | Yandex typo correction; set `false` for code, product names, jargon (it once turned "MCP" into "TCP") |
| `date` | str | Document date filter in `date:` operator syntax without the prefix, e.g. `>20250101` |
| `lang` | str | Document language, ISO 639-1 (`ru`, `en`) |
| `doc_format` | `pdf/doc/rtf/xls/ods/ppt/odp/odt/odg/swf` | Document file format |

Returns: `{answer, sources[{url, title, used}], search_queries, is_answer_rejected, is_bullet_answer, problematic_answer, fixed_misspell_query}`. Quota is **1 request/second**; responses take tens of seconds.

## Quotas

Defaults ([current limits](https://aistudio.yandex.ru/docs/en/search-api/concepts/limits)):

| Endpoint | RPS | Per hour |
|---|---|---|
| web / image | 10 | 10,000 |
| gen | 1 | 1,000 |

The server retries 429 and 5xx (3 attempts, exponential backoff, honoring `Retry-After` up to 10 s) but does not work around quotas. Generative search is **never** re-sent after a read timeout or a dropped connection — the request may already have been processed and billed.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `auth` (401/403) | Invalid key, missing scope `yc.search-api.execute` or role `search-api.editor` | Check the key and the service account's folder roles |
| `quota` (429) | RPS or hourly quota exceeded | Wait; gen is limited to 1 rps |
| `bad_request` (400) | Invalid parameters (e.g. query > 400 chars) | The API error text is included in the message |
| Startup fails immediately | `YANDEX_SEARCH_API_KEY`/`YANDEX_FOLDER_ID` not set | See the stderr message |
| A tool is missing | `YANDEX_MCP_ENABLED_TOOLS` hides it | Remove the variable or add the tool name |

## Development

```bash
.venv/bin/pip install -e ".[dev]"
make check          # ruff check + ruff format --check + pytest (67 tests on live fixtures)
```

Fixtures are re-captured with `scripts/capture_fixtures.py` (reads credentials from env or a local `keys.json`, which is gitignored).

Implementation notes baked into the parser (verified against live API responses):

- the generative endpoint returns a **JSON array** `[{...}]`, not a bare object;
- an empty result set arrives as `<error code="15">` inside the XML — the parser maps it to `results: []`, not an error;
- typo corrections arrive as `<reask>` (not `<misspell>`);
- `<found priority="...">` exists both at response level and inside groupings — only the response-level one is used.

## License

MIT
