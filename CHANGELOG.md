# Changelog

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] — 2026-09-25

### Fixed

- Tools are now `async` over `httpx.AsyncClient`. Sync tools ran inside FastMCP's event loop, so one slow `yandex_gen_search` (up to minutes with retries) froze pings, cancellation and parallel calls.
- `yandex_gen_search` is no longer re-sent after a read timeout or a dropped connection: the request may already have been processed and billed. Connection failures and 429/5xx are still retried.
- `has_more` is computed from the group count in `<grouping>`, so it stays correct with `dedupe_by_domain=true`.
- Unexpected server errors are logged with a traceback (stderr) instead of being silently wrapped.

### Added

- `yandex_gen_search`: `fix_typos` (typo correction can now be turned off), `url` scope, lists for `site`/`host`/`url` (API limits 5/5/10), `date`/`lang`/`doc_format` search filters. Response adds `search_queries`, `is_bullet_answer`, `problematic_answer`. Fields checked against the `GenSearchRequest` proto in `yandex-cloud/cloudapi`.
- 429 honors `Retry-After`; if it exceeds 10 s the error is returned right away instead of retrying a spent quota.
- Streamable HTTP transport: `YANDEX_MCP_TRANSPORT=http`, `YANDEX_MCP_HOST`, `YANDEX_MCP_PORT`.
- `YANDEX_MCP_DISABLED_TOOLS` blacklist (comma or space separated, like the whitelist); `YANDEX_SEARCH_API_KEY_FILE` for Docker/K8s secrets.
- `yandex-search-mcp` console script: runs via `uvx --from git+https://github.com/oleg-cat/yandex-search-mcp yandex-search-mcp`.
- CI: Python 3.13 and a Docker build job. Tests: 49 → 67 (concurrency, gen retry policy, Retry-After, config, entry point, client surviving HTTP sessions).

## [0.1.0] — 2026-06-11

### Added

- Three MCP tools over STDIO: `yandex_web_search`, `yandex_image_search`, `yandex_gen_search` (Yandex Search API v2), with fully typed parameters, structured output (outputSchema), `readOnlyHint` annotations, and LLM-facing descriptions with usage guidance.
- Full Russian-language search support: all 6 search types (`ru/com/tr/kk/be/uz`), numeric geo-id regions, localization auto-following the search type.
- Web search options: freshness period, sort by time, domain dedupe (`GROUP_MODE_DEEP`), typo correction toggle, family mode, Yandex query operators passed through (`site:`, `host:`, `date:`, quotes).
- Image search filters: format, size, orientation, color, site restriction. Responses contain URLs and metadata only — no base64 payloads.
- Generative search: synthesized answer with cited sources, `is_answer_rejected` and `fixed_misspell_query` transparency fields, site/host source restriction.
- Reliability: shared `httpx` client, retries with exponential backoff and jitter on 429/5xx/network errors only (3 attempts; higher base for gen due to its 1 rps quota), per-endpoint timeouts, unified JSON error contract `{"error": {type, message, retryable}}`.
- Security: secrets via env only, API key redacted from all logs and error messages (test-covered), `defusedxml` for untrusted XML, startup validation with clear stderr errors.
- Parser written against live API fixtures; notable behaviors handled: gen endpoint returns a JSON array, empty result arrives as XML `<error code="15">`, typo corrections arrive as `<reask>`, NBSP/soft-hyphen normalization, punycode domains decoded to Unicode.
- Tool whitelist via `YANDEX_MCP_ENABLED_TOOLS`.
- 49 tests (parsing on live fixtures, HTTP layer via respx, tool contracts), `make check` (ruff + pytest), Dockerfile (python:3.12-slim, non-root, STDIO), bilingual README with Claude Code / Codex CLI / opencode setup.
