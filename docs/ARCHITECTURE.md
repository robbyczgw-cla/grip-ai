# Grip AI — Architecture

## System Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                         Clients                                     │
│                                                                     │
│  ┌─────────────┐  ┌─────────────┐  ┌──────────┐  ┌─────────────┐  │
│  │  Telegram    │  │  GripCami   │  │ Discord  │  │   Slack     │  │
│  │  Bot API     │  │  (Web UI)   │  │  Bot     │  │  Socket     │  │
│  └──────┬───────┘  └──────┬──────┘  └────┬─────┘  └──────┬──────┘  │
└─────────┼─────────────────┼──────────────┼────────────────┼─────────┘
          │                 │              │                │
          │   HTTP/SSE      │  HTTP/SSE    │  WS            │  WS
          ▼                 ▼              ▼                ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    Grip Gateway (:18800)                             │
│                                                                     │
│  ┌───────────────────────┐  ┌──────────────────────────────────┐   │
│  │   REST API (FastAPI)   │  │       Channel Adapters            │   │
│  │                        │  │                                   │   │
│  │  /api/v1/chat          │  │  telegram.py ── voice STT/TTS    │   │
│  │  /api/v1/info          │  │  discord.py  ── attachments      │   │
│  │  /api/v1/memory/*      │  │  slack.py    ── socket mode      │   │
│  │  /api/v1/cron          │  │                                   │   │
│  │  /api/v1/sessions      │  └───────────────┬──────────────────┘   │
│  │  /api/v1/tools         │                  │                      │
│  └───────────┬────────────┘                  │                      │
│              │                               │                      │
│              ▼                               ▼                      │
│  ┌──────────────────────────────────────────────────────────┐      │
│  │                  Message Bus (asyncio.Queue)              │      │
│  └──────────────────────────┬───────────────────────────────┘      │
│                             │                                       │
│                             ▼                                       │
│  ┌──────────────────────────────────────────────────────────┐      │
│  │                   SDK Engine (SDKRunner)                   │      │
│  │                                                           │      │
│  │  ┌─────────────────────────────────────────────────────┐ │      │
│  │  │          Claude Agent SDK (ClaudeSDKClient)          │ │      │
│  │  │                                                      │ │      │
│  │  │  • Agentic loop with tool execution                  │ │      │
│  │  │  • allowed_tools = None (all native tools enabled)   │ │      │
│  │  │  • Native: WebSearch, WebFetch, Read, Write, Edit,   │ │      │
│  │  │    Bash, Glob, Grep, AskUserQuestion                 │ │      │
│  │  │  • Subagent telemetry hooks                          │ │      │
│  │  └─────────────────────────────────────────────────────┘ │      │
│  │                                                           │      │
│  │  ┌─────────────────────┐  ┌───────────────────────────┐ │      │
│  │  │   Custom @tool Fns   │  │    System Prompt Builder   │ │      │
│  │  │                      │  │                            │ │      │
│  │  │  remember / recall   │  │  Identity files (AGENT.md, │ │      │
│  │  │  semantic_recall     │  │    SOUL.md, USER.md, etc.) │ │      │
│  │  │  get_weather         │  │  Memory search results     │ │      │
│  │  │  youtube_transcript  │  │  History search results    │ │      │
│  │  │  twitter_search      │  │  KB learned patterns       │ │      │
│  │  │  web_search          │  │  Skills                    │ │      │
│  │  │  summarize_today     │  │  Tool descriptions         │ │      │
│  │  │  summarize_month     │  └───────────────────────────┘ │      │
│  │  │  send_message/file   │                                │      │
│  │  └─────────────────────┘                                 │      │
│  └──────────────────────────────────────────────────────────┘      │
│                                                                     │
│  ┌────────────────┐  ┌───────────────┐  ┌────────────────────┐     │
│  │  Cron Service   │  │  Session Mgr  │  │  Heartbeat Service │     │
│  │  (croniter)     │  │  (JSON/LRU)   │  │  (periodic wake)   │     │
│  └────────────────┘  └───────────────┘  └────────────────────┘     │
└─────────────────────────────────────────────────────────────────────┘
```

## Component Details

### SDK Engine (`grip/engines/sdk_engine.py`)

The primary engine. Wraps `ClaudeSDKClient` from the Claude Agent SDK. Grip provides:
- **System prompt** assembled from identity files, memory search, history, knowledge base, and skills
- **Custom tools** registered via `@tool` decorator (same as SDK native tools)
- **MCP server config** translated from grip format to SDK format
- **History persistence** via MemoryManager

Key design: `allowed_tools=None` unlocks all native Claude SDK tools (WebSearch, WebFetch, Read, Write, Edit, Bash, Glob, Grep). Custom tools extend this set.

### Memory System

Four interconnected layers:

```
┌─────────────────────────────────────────────────────┐
│                  Memory System                       │
│                                                      │
│  ┌──────────────┐         ┌────────────────────┐    │
│  │  MEMORY.md   │◄────────│  remember() tool   │    │
│  │  (TF-IDF)    │         │  dual-writes to    │    │
│  └──────────────┘         │  both stores       │    │
│                           └────────┬───────────┘    │
│  ┌──────────────┐                  │                │
│  │  HISTORY.md  │                  ▼                │
│  │  (time-decay)│         ┌────────────────────┐    │
│  └──────────────┘         │  ChromaDB          │    │
│                           │  (semantic vectors) │    │
│  ┌──────────────┐         │                    │    │
│  │  Archives    │         │  Groq nomic-embed  │    │
│  │  daily/      │◄───┐    │  -text-v1_5        │    │
│  │  monthly/    │    │    └────────────────────┘    │
│  └──────────────┘    │                              │
│                      │    ┌────────────────────┐    │
│                      └────│  Archiver          │    │
│                           │  (Groq LLM summary)│    │
│                           └────────────────────┘    │
└─────────────────────────────────────────────────────┘
```

| Layer | File/Store | Search Method | Purpose |
|-------|-----------|---------------|---------|
| **MEMORY.md** | `~/.grip/workspace/MEMORY.md` | TF-IDF + Jaccard dedup | Durable facts, identity |
| **HISTORY.md** | `~/.grip/workspace/HISTORY.md` | Time-decay weighted | Conversation summaries |
| **ChromaDB** | `~/.grip/memory/chroma/` | Cosine similarity (vectors) | Semantic recall |
| **Archives** | `~/.grip/memory/daily/`, `monthly/` | Direct file read | Daily/monthly summaries |

#### Semantic Indexing Flow

1. User says "remember X" → `remember()` tool fires
2. Fact appended to MEMORY.md (upstream behavior, unchanged)
3. Best-effort: text embedded via Groq `nomic-embed-text-v1_5` → stored in ChromaDB
4. If ChromaDB/Groq fails, MEMORY.md write still succeeds (non-breaking)

#### Archive Generation Flow

1. Cron fires `daily-memory-archive` at 23:00
2. `archiver.py` collects today's HISTORY.md entries + MEMORY.md additions
3. Groq LLM generates summary → saved to `~/.grip/memory/daily/YYYY-MM-DD.md`
4. Monthly: aggregates daily summaries → `~/.grip/memory/monthly/YYYY-MM.md`

### Voice Pipeline (Telegram)

```
Voice message received
        │
        ▼
  Download .oga file
        │
        ▼
  ┌─────────────────────────┐
  │  ElevenLabs Scribe v1   │──── fails? ────┐
  │  POST /v1/speech-to-text│                 │
  └────────────┬────────────┘                 ▼
               │                    ┌────────────────────┐
               │                    │  Groq STT fallback  │
               │                    │  whisper-large-v3   │
               │                    └─────────┬──────────┘
               ▼                              ▼
        Transcript text injected into chat pipeline
               │
               ▼
        SDK Engine processes normally
               │
               ▼
        Text reply sent to Telegram
               │
               ▼ (if tts_enabled)
        ElevenLabs TTS (Jessica voice)
        Audio sent as additional voice message
```

### Tool Categories

| Category | Tools | Module |
|----------|-------|--------|
| **Memory** | remember, recall, semantic_recall, summarize_today, summarize_month, list_memory_archives, read_memory_archive | `sdk_engine.py`, `memory/semantic.py`, `memory/archiver.py` |
| **Web Search** | web_search, web_search_plus | `tools/web.py`, `tools/web_search_plus.py` |
| **Media** | youtube_transcript, twitter_search | `sdk_engine.py` (Apify actors) |
| **Utility** | get_weather | `sdk_engine.py` (wttr.in) |
| **Communication** | send_message, send_file | `sdk_engine.py` |
| **File System** | read, write, edit, list, delete, trash, append | `tools/filesystem.py` + SDK native |
| **Shell** | exec (with deny-list) | `tools/shell.py` + SDK native Bash |
| **Finance** | stock_quote, stock_history, company_info | `tools/finance.py` |
| **Research** | web_research, convert_document | `tools/research.py`, `tools/markitdown.py` |
| **Orchestration** | spawn subagent, cron scheduling | `tools/spawn.py`, `tools/scheduler.py` |

### Web Search Provider Routing

**`web_search`** (simple fallback chain):
```
Brave Search → Serper (Google) → DuckDuckGo
```

**`web_search_plus`** (intent-based routing):
```
Query analyzed for intent:
  ├── News/current events → Perplexity
  ├── Academic/research   → Exa (neural search)
  ├── General factual     → Tavily
  └── Default             → Serper (Google)
```

### Caching

| Cache | Location | Purpose |
|-------|----------|---------|
| YouTube transcripts | `~/.grip/cache/youtube/<video_id>.json` | Avoid re-fetching transcripts |
| Twitter results | `~/.grip/cache/twitter/` | Avoid re-fetching search results |
| Semantic cache | In-memory (SHA-256 keyed, TTL) | Response caching |
| Session cache | In-memory LRU (200 entries) | Session data |

### File Structure (Fork Additions)

```
grip/
├── engines/
│   └── sdk_engine.py          # SDKRunner + all custom @tool functions
├── memory/
│   ├── semantic.py            # SemanticMemory (ChromaDB + Groq embeddings)
│   ├── archiver.py            # Daily/monthly archive generation
│   ├── manager.py             # MemoryManager (upstream)
│   ├── knowledge_base.py      # KnowledgeBase (upstream)
│   ├── semantic_cache.py      # Response cache (upstream)
│   └── pattern_extractor.py   # Behavioral patterns (upstream)
├── tools/
│   ├── web_search_plus.py     # Multi-provider search (fork addition)
│   └── ... (upstream tools)
├── channels/
│   └── telegram.py            # Voice STT/TTS additions
└── api/
    └── management.py          # /info, /memory/archives, /memory/archive endpoints

~/.grip/
├── config.json
├── workspace/
│   ├── MEMORY.md
│   └── HISTORY.md
├── memory/
│   ├── chroma/                # ChromaDB persistent store
│   ├── daily/                 # Daily archive summaries
│   └── monthly/               # Monthly digests
└── cache/
    ├── youtube/               # Transcript cache
    └── twitter/               # Search result cache
```
