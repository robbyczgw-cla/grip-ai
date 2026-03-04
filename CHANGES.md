# Grip AI Fork — Changes vs Upstream

> Fork: `robbyczgw-cla/grip-ai`  
> Upstream: original grip-ai  
> Last updated: 2026-03-04

---

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

