# Grip AI Fork — Changes vs Upstream

> Fork: `robbyczgw-cla/grip-ai`  
> Upstream: original grip-ai  
> Last updated: 2026-03-04

---


## 2026-03-04: /info Command + Info API Endpoint

### Telegram /info command
- Rich system status dashboard via  command in Telegram
- Shows: model + effort, uptime + version, memory stats, cron jobs,
  tools/skills loaded, API key status, cache stats, Graz weather, today's activity
- Auto-fetches from new  endpoint + wttr.in weather

### API: GET /api/v1/info
- New comprehensive info endpoint in management router
- Aggregates: model config, uptime, memory size, history count, cron jobs,
  skills list, API key status (from config file), cache stats, session count,
  today's messages, last memory update, channel status
- Used by both Telegram /info and GripCami dashboard


## 🔍 Web Search

### Serper as Search Fallback (commit `09ae644`)
- Added Serper (Google) as web search provider between Brave and DuckDuckGo
- Config: `tools.extra.serper_api_key`
- Priority: Brave → Serper → DuckDuckGo

### web_search_plus — Multi-Provider Routing (in progress)
- New module: `grip/tools/web_search_plus.py`
- Providers: Serper / Tavily / Exa / Perplexity with auto-routing by query intent
- Config keys: `serper_api_key`, `tavily_api_key`, `exa_api_key`, `perplexity_api_key`

---

## 🛠️ Claude Agent SDK

### All Built-in Tools Enabled (commit `377f5ce`)
- `allowed_tools=None` — alle nativen SDK Tools freigegeben
- Read, Write, Edit, Bash, Glob, Grep, **WebSearch**, **WebFetch**, AskUserQuestion

### Subagent Telemetry Hooks (in progress)
- SubagentStart/Stop Hooks in `sdk_engine.py`
- Logging + optional session history entry bei Subagent-Aktivität

### Native Web Search Attempt (commit `9f0497c`) — NOT WORKING
- `enable_native_search` config flag hinzugefügt
- `web_search_20250305` Tool Type — funktioniert nicht über claude_agent_sdk Stack
- Bleibt als Config-Flag erhalten, hat keinen Effekt

---

## 🧠 Memory

- Dual-layer Memory (MEMORY.md + HISTORY.md) via MemoryManager — upstream feature, unverändert
- `/api/v1/memory` → read MEMORY.md content
- `/api/v1/memory/search?q=` → TF-IDF search über History ✅

---

## ⚙️ Adaptive Thinking (commits `ca1c0f9` – `7e86f9a`)
- `sdk_effort` config (low/medium/high) → steuert Claude's Thinking-Level
- `/status` Bot-Command zeigt Thinking-Level
- Default: `medium`

---

## 📡 Telegram
- Channel aktiv, Token in config
- Bot: @Robgripbot


## 🎙️ Telegram Voice + ElevenLabs

### Voice message transcription (new)
- Telegram `on_voice()` now downloads incoming voice files and transcribes with **ElevenLabs Scribe v1** (`/v1/speech-to-text`)
- Transcript is injected into normal inbound chat flow (same path as user-typed text)
- Friendly user-facing error messages on missing API key / failed transcription
- Config key: `tools.extra.elevenlabs_api_key`

### Optional voice replies via ElevenLabs TTS (new)
- Telegram outbound `send()` supports optional TTS mode
- When `tools.extra.tts_enabled=true`, bot synthesizes reply audio with ElevenLabs and sends as Telegram voice message
- Voice: **Jessica** (`cgSgspJ2msm6clMCkdW9`)
- Default remains text replies (`tts_enabled=false`)

---

## 🛠 Native Tools Added (2026-03-04)

Added 3 new SDK-native tools in `grip/engines/sdk_engine.py` (registered with `@tool`, same pattern as `remember/recall/web_search`):

- `get_weather(location: str, days: int = 1)`
  - Uses `wttr.in` JSON API (`?format=j1`)
  - Returns current temperature, conditions, humidity, wind, and multi-day forecast summary

- `youtube_transcript(video_url: str)`
  - Uses Apify actor `bernardo_apartado~youtube-transcript-downloader`
  - Polls run status until completion, then fetches transcript text from dataset
  - Caches transcripts in `~/.grip/cache/youtube/<video_id>.json`
  - Reads token from `tools.extra.apify_api_token` (fallback: `APIFY_API_TOKEN` env)

- `twitter_search(query: str, max_results: int = 10)`
  - Uses Apify Twitter actor `CJdippxWmn9uRfooo`
  - Supports search query, `@username`, and profile/status URL input patterns
  - Caches rendered results in `~/.grip/cache/twitter/`
  - Reads token from `tools.extra.apify_api_token` (fallback: `APIFY_API_TOKEN` env)

Also updated `/root/.grip/config.json` to include `tools.extra.apify_api_token` and created cache directories:
- `~/.grip/cache/youtube/`
- `~/.grip/cache/twitter/`
