# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2026-03-04

First major release of the Grip AI fork. Massive feature day covering semantic memory,
voice I/O, multi-provider web search, custom SDK tools, and API extensions.

### Added

#### Semantic Memory System
- **ChromaDB vector store** at `~/.grip/memory/chroma/` with persistent collection `grip_memory`
- **Groq embeddings** via `nomic-embed-text-v1_5` model for semantic indexing
- `grip/memory/semantic.py` — `SemanticMemory` class with `add()`, `search()`, `count()` methods
- `grip/memory/archiver.py` — daily and monthly archive generation via Groq LLM
- `semantic_recall` tool — vector similarity search over memory
- `summarize_today` tool — generate daily memory summary
- `summarize_month` tool — generate monthly memory digest from daily archives
- `list_memory_archives` / `read_memory_archive` tools — browse archive files
- `remember` tool now performs best-effort semantic indexing alongside MEMORY.md append
- Memory directories: `~/.grip/memory/chroma/`, `~/.grip/memory/daily/`, `~/.grip/memory/monthly/`
- Cron job `daily-memory-archive` (23:00 daily) for automatic daily summaries
- Cron job `monthly-memory-digest` (10:00 on 1st of month) for monthly digests

#### Voice I/O (Telegram)
- **ElevenLabs Scribe v1** speech-to-text for Telegram voice messages
- **Groq STT fallback** using `whisper-large-v3-turbo` when ElevenLabs is unavailable
- **ElevenLabs TTS** (Jessica voice, `cgSgspJ2msm6clMCkdW9`) for Telegram voice replies
- TTS is opt-in via `tools.extra.tts_enabled` — sends audio in addition to text (not replacing)

#### Web Search
- **Serper** (Google Search) as additional provider in search fallback chain (Brave → Serper → DuckDuckGo)
- **`web_search_plus`** module (`grip/tools/web_search_plus.py`) — multi-provider search with auto-routing by query intent
  - Providers: Serper, Tavily, Exa, Perplexity
  - Config keys: `serper_api_key`, `tavily_api_key`, `exa_api_key`, `perplexity_api_key`

#### Custom SDK Tools
- `get_weather(location, days)` — current conditions + forecast via wttr.in JSON API (no API key required)
- `youtube_transcript(video_url)` — fetch YouTube transcripts via Apify actor, with `~/.grip/cache/youtube/` caching
- `twitter_search(query, max_results)` — search X/Twitter via Apify actor, with `~/.grip/cache/twitter/` caching

#### Claude Agent SDK
- **All native SDK tools enabled** (`allowed_tools=None`) — WebSearch, WebFetch, Read, Write, Edit, Bash, Glob, Grep, AskUserQuestion
- **Subagent telemetry hooks** — SubagentStart/SubagentStop logging in `sdk_engine.py`

#### Telegram
- `/info` bot command — comprehensive system status dashboard showing model, uptime, memory stats, crons, tools, API key status, cache stats, Graz weather, today's activity

#### API Endpoints
- `GET /api/v1/info` — aggregated system info with semantic memory stats (ChromaDB entries, archive counts, latest daily)
- `GET /api/v1/memory/archives?type=daily|monthly` — list archive files with metadata
- `GET /api/v1/memory/archive?type=daily|monthly&date=...` — read specific archive content

### Changed

- Search fallback chain expanded: Brave → **Serper** → DuckDuckGo
- `remember` tool now dual-writes to MEMORY.md + ChromaDB (non-breaking fallback)
- TTS behavior: voice audio sent alongside text reply (not replacing it)
- `/api/v1/info` response extended with `semantic_memory` block

### Fixed

- Graceful fallback when ChromaDB or Groq embeddings are unavailable (no crashes)
- Archive endpoint validation and non-destructive error handling

---

*For upstream grip-ai changes, see the [original repository](https://github.com/5unnykum4r/grip-ai).*
