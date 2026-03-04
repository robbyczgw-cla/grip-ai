<div align="center">
  <h1>🤖 Grip AI — Personal AI Agent Platform (Fork)</h1>
  <p>Claude Agent SDK powered • Semantic Memory • Multi-Channel • Self-Hosted</p>
  <p>
    <a href="#whats-different"><strong>What's Different</strong></a> &nbsp;·&nbsp;
    <a href="#quickstart"><strong>Quick Start</strong></a> &nbsp;·&nbsp;
    <a href="#tools-reference"><strong>Tools</strong></a> &nbsp;·&nbsp;
    <a href="#api-reference"><strong>API</strong></a> &nbsp;·&nbsp;
    <a href="#configuration"><strong>Config</strong></a> &nbsp;·&nbsp;
    <a href="#architecture"><strong>Architecture</strong></a>
  </p>
</div>

<p align="center">
  <img src="https://img.shields.io/badge/engine-Claude%20Agent%20SDK-blueviolet" alt="Claude Agent SDK">
  <img src="https://img.shields.io/badge/python-3.12%2B-blue" alt="Python 3.12+">
  <img src="https://img.shields.io/badge/memory-ChromaDB%20Semantic-orange" alt="Semantic Memory">
  <img src="https://img.shields.io/badge/license-MIT-green" alt="MIT License">
</p>

---

> **Fork of [grip-ai](https://github.com/5unnykum4r/grip-ai)** by [@robbyczgw-cla](https://github.com/robbyczgw-cla)
>
> This fork extends upstream grip-ai with semantic memory (ChromaDB), multi-provider web search, voice I/O (ElevenLabs/Groq), custom SDK tools, memory archiving, and a companion web UI ([GripCami](https://github.com/robbyczgw-cla/gripcami)).

## <a id="whats-different"></a>What's Different from Upstream

| Area | Upstream grip-ai | This Fork |
|------|-----------------|-----------|
| **Memory** | MEMORY.md + HISTORY.md (TF-IDF) | + ChromaDB semantic memory, daily/monthly archives, cron-driven summarization |
| **Web Search** | Brave + DuckDuckGo | + Serper, `web_search_plus` with Tavily/Exa/Perplexity auto-routing |
| **Voice (STT)** | — | ElevenLabs Scribe v1, Groq whisper-large-v3-turbo fallback |
| **Voice (TTS)** | — | ElevenLabs Jessica voice (opt-in, text + audio) |
| **Custom Tools** | Standard grip tools | + `get_weather`, `youtube_transcript`, `twitter_search`, `semantic_recall`, `summarize_today`, `summarize_month` |
| **SDK Tools** | Limited | All native tools enabled (WebSearch, WebFetch, Read, Write, Edit, Bash, Glob, Grep) |
| **Telemetry** | — | Subagent start/stop hooks in SDK engine |
| **Telegram** | Basic | + `/info` command with full system dashboard |
| **API** | Standard endpoints | + `/api/v1/info`, `/api/v1/memory/archives`, `/api/v1/memory/archive` |
| **Web UI** | — | [GripCami](https://github.com/robbyczgw-cla/gripcami) companion app |

## <a id="quickstart"></a>Quick Start

### Prerequisites

- Python 3.12+, [uv](https://docs.astral.sh/uv/)
- Anthropic API key
- ChromaDB (`pip install chromadb`) for semantic memory
- (Optional) API keys for extended tools — see [Config Reference](#config-keys-reference)

### Install & Run

```bash
# Clone the fork
git clone https://github.com/robbyczgw-cla/grip-ai.git
cd grip-ai

# Install dependencies
uv sync

# Run setup wizard
grip onboard

# Start the gateway (API + Telegram + crons)
grip gateway
```

### Minimal Config (`~/.grip/config.json`)

```json
{
  "engine": "claude_sdk",
  "sdk_model": "claude-sonnet-4-6",
  "providers": {
    "anthropic": { "api_key": "sk-ant-..." }
  },
  "channels": {
    "telegram": {
      "enabled": true,
      "token": "YOUR_BOT_TOKEN",
      "allow_from": ["YOUR_TELEGRAM_USER_ID"]
    }
  },
  "gateway": {
    "api": { "auth_token": "grip_YOUR_TOKEN" }
  },
  "tools": {
    "extra": {
      "elevenlabs_api_key": "",
      "groq_api_key": "",
      "apify_api_token": "",
      "serper_api_key": "",
      "tavily_api_key": "",
      "exa_api_key": "",
      "perplexity_api_key": "",
      "tts_enabled": false
    }
  }
}
```

## <a id="architecture"></a>Architecture

```
┌─────────────┐  ┌─────────────┐
│  Telegram    │  │  GripCami   │
│  (Bot API)   │  │  (Web UI)   │
└──────┬───────┘  └──────┬──────┘
       │                 │
       │    HTTP/SSE     │
       ▼                 ▼
┌──────────────────────────────────────────────────┐
│              Grip Gateway (:18800)                │
│                                                  │
│  ┌──────────┐  ┌───────────┐  ┌──────────────┐  │
│  │ REST API │  │ Msg Bus   │  │ Cron Service │  │
│  │ (FastAPI)│  │ (asyncio) │  │ (croniter)   │  │
│  └────┬─────┘  └─────┬─────┘  └──────┬───────┘  │
│       │              │               │           │
│       ▼              ▼               ▼           │
│  ┌──────────────────────────────────────────┐    │
│  │           SDK Engine (SDKRunner)          │    │
│  │  ┌─────────────────────────────────────┐ │    │
│  │  │     Claude Agent SDK (agentic loop) │ │    │
│  │  │  • allowed_tools = None (all native)│ │    │
│  │  │  • WebSearch, Bash, Read, Write, …  │ │    │
│  │  └─────────────────────────────────────┘ │    │
│  │  ┌──────────────┐  ┌──────────────────┐  │    │
│  │  │ Custom Tools  │  │  Memory System   │  │    │
│  │  │ (see below)   │  │  (see below)     │  │    │
│  │  └──────────────┘  └──────────────────┘  │    │
│  └──────────────────────────────────────────┘    │
└──────────────────────────────────────────────────┘

Memory System:
┌──────────────────────────────────────────────┐
│  MEMORY.md ──── durable facts (TF-IDF)       │
│  HISTORY.md ─── timestamped logs (decay)     │
│  ChromaDB ───── semantic vectors (Groq embed)│
│  ~/.grip/memory/daily/   ─── daily summaries │
│  ~/.grip/memory/monthly/ ─── monthly digests │
└──────────────────────────────────────────────┘
```

For a detailed architecture breakdown, see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## <a id="tools-reference"></a>Tools Reference

### Custom Tools (Fork Additions)

| Tool | Description | Required Config Key |
|------|-------------|-------------------|
| `get_weather` | Current weather + forecast via wttr.in | — (no API key) |
| `youtube_transcript` | Fetch YouTube video transcripts via Apify | `tools.extra.apify_api_token` |
| `twitter_search` | Search X/Twitter posts via Apify | `tools.extra.apify_api_token` |
| `semantic_recall` | Vector similarity search over memory (ChromaDB) | `tools.extra.groq_api_key` |
| `summarize_today` | Generate daily memory summary via LLM | `tools.extra.groq_api_key` |
| `summarize_month` | Generate monthly memory digest via LLM | `tools.extra.groq_api_key` |
| `list_memory_archives` | List available daily/monthly archives | — |
| `read_memory_archive` | Read content of a specific archive | — |
| `web_search` | Web search with Brave → Serper → DuckDuckGo fallback | `tools.extra.serper_api_key` (optional) |
| `web_search_plus` | Multi-provider search with intent routing | `tools.extra.tavily_api_key`, `exa_api_key`, `perplexity_api_key` |

### Built-in Tools (from SDK + Upstream)

| Tool | Description |
|------|-------------|
| `remember` | Save fact to MEMORY.md + semantic index |
| `recall` | TF-IDF keyword search over memory |
| `send_message` | Send message to active channel |
| `send_file` | Send file attachment to active channel |
| WebSearch | Native Claude SDK web search |
| WebFetch | Native Claude SDK URL fetching |
| Read / Write / Edit | Native Claude SDK file operations |
| Bash | Native Claude SDK shell execution |
| Glob / Grep | Native Claude SDK file search |

### Voice I/O (Telegram)

| Feature | Provider | Config Key |
|---------|----------|-----------|
| **Speech-to-Text** | ElevenLabs Scribe v1 (primary) | `tools.extra.elevenlabs_api_key` |
| **STT Fallback** | Groq whisper-large-v3-turbo | `tools.extra.groq_api_key` |
| **Text-to-Speech** | ElevenLabs (Jessica voice) | `tools.extra.elevenlabs_api_key` + `tts_enabled: true` |

## <a id="api-reference"></a>API Reference

All endpoints require `Authorization: Bearer <token>` unless noted.

### Fork-Added Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/info` | Comprehensive system status: model, uptime, memory stats (incl. ChromaDB), cron jobs, tools, API key status, cache, session count |
| GET | `/api/v1/memory/archives?type=daily\|monthly` | List archive files with date, size, last modified |
| GET | `/api/v1/memory/archive?type=daily\|monthly&date=YYYY-MM-DD` | Read specific archive content |

### Upstream Endpoints (Selected)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Health probe (no auth) |
| POST | `/api/v1/chat` | Send message, get response (blocking + SSE) |
| GET | `/api/v1/sessions` | List sessions |
| DELETE | `/api/v1/sessions/{key}` | Delete session |
| GET | `/api/v1/tools` | List available tools |
| GET | `/api/v1/memory` | Read MEMORY.md |
| GET | `/api/v1/memory/search?q=...` | TF-IDF search over history |
| GET/POST/DELETE | `/api/v1/cron` | Manage cron jobs |
| GET | `/api/v1/metrics` | Runtime metrics |
| GET | `/api/v1/config` | Masked config dump |
| GET | `/api/v1/status` | System status |

### Telegram Bot Commands

| Command | Description |
|---------|-------------|
| `/start` | Welcome message |
| `/help` | List commands |
| `/new` | Fresh conversation |
| `/status` | Session info |
| `/info` | **Full system dashboard** (model, uptime, memory, crons, weather, activity) |
| `/model <name>` | Switch model |
| `/compact` | Compress session |
| `/undo` | Remove last exchange |
| `/clear` | Clear history |
| `/trust <path>` | Grant directory access |

## <a id="config-keys-reference"></a>Config Keys Reference (`tools.extra`)

| Key | Description | Required For |
|-----|-------------|-------------|
| `elevenlabs_api_key` | ElevenLabs API key | Voice STT/TTS |
| `groq_api_key` | Groq API key | STT fallback, semantic embeddings (nomic-embed-text-v1_5), archive summaries |
| `apify_api_token` | Apify platform token | `youtube_transcript`, `twitter_search` |
| `serper_api_key` | Serper (Google Search) API key | `web_search` Serper fallback |
| `tavily_api_key` | Tavily search API key | `web_search_plus` |
| `exa_api_key` | Exa neural search API key | `web_search_plus` |
| `perplexity_api_key` | Perplexity API key | `web_search_plus` |
| `tts_enabled` | Enable TTS voice replies (`true`/`false`) | ElevenLabs TTS |

## Memory System

### Three Layers

1. **MEMORY.md** — Durable facts, TF-IDF searchable. The `remember` tool writes here.
2. **HISTORY.md** — Timestamped conversation summaries with time-decay search.
3. **ChromaDB** — Semantic vector store at `~/.grip/memory/chroma/`. Every `remember` call also indexes into ChromaDB for similarity search via `semantic_recall`.

### Archives

- **Daily summaries** (`~/.grip/memory/daily/YYYY-MM-DD.md`) — Generated by `summarize_today` tool or the `daily-memory-archive` cron (runs at 23:00).
- **Monthly digests** (`~/.grip/memory/monthly/YYYY-MM.md`) — Generated by `summarize_month` tool or the `monthly-memory-digest` cron (runs 1st of each month at 10:00).

### Embeddings

Embeddings are generated via **Groq** using the `nomic-embed-text-v1_5` model. Archive summaries are also generated through Groq's LLM API.

## Cron Jobs

Pre-configured cron jobs:

| Name | Schedule | Description |
|------|----------|-------------|
| `daily-memory-archive` | `0 23 * * *` | Generate daily memory summary |
| `monthly-memory-digest` | `0 10 1 * *` | Generate monthly digest from daily archives |

Manage via CLI (`grip cron list/add/remove`) or API (`/api/v1/cron`).

## Development

```bash
uv sync --group dev
uv run ruff check grip/ tests/
uv run pytest
```

## License

MIT — see upstream [grip-ai](https://github.com/5unnykum4r/grip-ai) for original license.
