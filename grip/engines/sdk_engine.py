"""SDKRunner — EngineProtocol implementation using ClaudeSDKClient.

This is the PRIMARY engine for Claude models. It delegates all tool execution,
agentic looping, and context management to the Claude Agent SDK. Grip handles:
  - System prompt assembly (identity files, memory, skills)
  - Custom tools (send_message, send_file, remember, recall)
  - MCP server config translation from grip format to SDK format
  - History persistence via MemoryManager

Uses ClaudeSDKClient (not query()) because custom tools created with the
@tool decorator require the client — query() does not support them.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from collections.abc import Mapping

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    HookMatcher,
    ClaudeSDKClient,
    CLIConnectionError,
    ResultMessage,
    create_sdk_mcp_server,
    tool,
)
from loguru import logger

from grip.engines.types import AgentRunResult, EngineProtocol
from grip.skills.loader import SkillsLoader

if TYPE_CHECKING:
    from grip.config.schema import GripConfig
    from grip.memory import MemoryManager
    from grip.memory.knowledge_base import KnowledgeBase
    from grip.session import SessionManager
    from grip.trust import TrustManager
    from grip.workspace import WorkspaceManager


class SDKRunner(EngineProtocol):
    """EngineProtocol implementation that uses ClaudeSDKClient for agentic runs.

    Unlike LiteLLMRunner (which wraps the internal AgentLoop), SDKRunner delegates
    the full agent loop to the Claude Agent SDK. Grip only provides the system
    prompt, custom tools, and MCP server configuration.
    """

    def __init__(
        self,
        config: GripConfig,
        workspace: WorkspaceManager,
        session_mgr: SessionManager,
        memory_mgr: MemoryManager,
        trust_mgr: TrustManager | None = None,
        knowledge_base: KnowledgeBase | None = None,
    ) -> None:
        self._config = config
        self._workspace = workspace
        self._session_mgr = session_mgr
        self._memory_mgr = memory_mgr
        self._trust_mgr = trust_mgr
        self._kb = knowledge_base

        # Resolve ANTHROPIC_API_KEY: config providers take priority, then env var.
        # Store it privately instead of writing to os.environ to prevent
        # exfiltration via child processes or shell commands.
        self._api_key = ""
        anthropic_provider = config.providers.get("anthropic")
        if anthropic_provider:
            self._api_key = anthropic_provider.api_key.get_secret_value()
        if not self._api_key:
            self._api_key = os.environ.get("ANTHROPIC_API_KEY", "")

        defaults = config.agents.defaults
        self._model: str = defaults.sdk_model
        self._permission_mode: str = defaults.sdk_permission_mode
        self._cwd: str = str(workspace.root)
        self._mcp_servers = config.tools.mcp_servers
        self._send_callback: Callable | None = None
        self._send_file_callback: Callable | None = None

        # Pre-build static artifacts once (MCP config and custom tools don't
        # change after construction).
        self._custom_tools: list = self._build_custom_tools()
        self._mcp_config: list[dict[str, Any]] = self._build_mcp_config()
        self._allowed_tools_base: list[str] = self._collect_allowed_tools()
        self._skills_cache: list = self._load_skills()

    # -- Callback wiring (called by gateway to route messages to channels) --

    def set_send_callback(self, callback: Callable) -> None:
        """Register the callback for send_message tool invocations."""
        self._send_callback = callback

    def set_send_file_callback(self, callback: Callable) -> None:
        """Register the callback for send_file tool invocations."""
        self._send_file_callback = callback

    # -- MCP config translation --

    def _build_mcp_config(self) -> list[dict[str, Any]]:
        """Convert grip MCPServerConfig entries to SDK-compatible dicts.

        Skips disabled servers. URL-based servers produce:
        {"name": ..., "url": ..., "headers": ..., "type": ...}
        Stdio-based servers produce:
        {"name": ..., "command": ..., "args": ..., "env": ...}
        """
        result: list[dict[str, Any]] = []
        for name, srv in self._mcp_servers.items():
            if not srv.enabled:
                continue
            if srv.url:
                entry: dict[str, Any] = {
                    "name": name,
                    "url": srv.url,
                    "headers": dict(srv.headers),
                }
                if srv.type:
                    entry["type"] = srv.type
                result.append(entry)
            elif srv.command:
                result.append(
                    {
                        "name": name,
                        "command": srv.command,
                        "args": list(srv.args),
                        "env": dict(srv.env),
                    }
                )
        return result

    def _collect_allowed_tools(self) -> list[str]:
        """Merge allowed_tools from all enabled MCP servers into a flat list."""
        tools: list[str] = []
        for _name, srv in self._mcp_servers.items():
            if not srv.enabled:
                continue
            tools.extend(srv.allowed_tools)
        return tools

    def _load_skills(self) -> list:
        """Load and cache available skills from the workspace."""
        try:
            loader = SkillsLoader(self._workspace.root)
            return loader.scan()
        except Exception as exc:
            logger.debug("Failed to load skills for system prompt: {}", exc)
            return []

    # -- System prompt assembly --

    def _build_system_prompt(
        self, user_message: str, session_key: str, custom_tools: list | None = None,
    ) -> str:
        """Assemble the system prompt from identity files, memory, skills, and metadata.

        Parts are joined with markdown horizontal rules for clear separation.
        Missing identity files are silently skipped.
        """
        parts: list[str] = []

        # Load identity files (AGENT.md, IDENTITY.md, SOUL.md, USER.md)
        identity_files = self._workspace.read_identity_files()
        for filename, content in identity_files.items():
            parts.append(f"## {filename}\n\n{content}")

        # Search long-term memory for relevant facts
        memory_results = self._memory_mgr.search_memory(user_message, max_results=5)
        if memory_results:
            memory_text = "\n".join(f"- {fact}" for fact in memory_results)
            parts.append(f"## Relevant Memory\n\n{memory_text}")

        # Search conversation history for relevant past interactions
        history_results = self._memory_mgr.search_history(user_message, max_results=5)
        if history_results:
            history_text = "\n".join(f"- {entry}" for entry in history_results)
            parts.append(f"## Relevant History\n\n{history_text}")

        # Inject learned behavioral patterns from KnowledgeBase (≤800 chars)
        if self._kb and self._kb.count > 0:
            kb_context = self._kb.export_for_context(max_chars=800)
            if kb_context:
                parts.append(f"## Learned Patterns\n\n{kb_context}")

        # List custom tools so the agent knows what it can call
        if custom_tools:
            tool_lines = [
                f"- **{t.name}**: {t.description}" for t in custom_tools
                if hasattr(t, "name") and hasattr(t, "description")
            ]
            if tool_lines:
                parts.append(
                    "## Available Tools\n\n"
                    "Use these tools to fulfil requests — prefer live tool "
                    "calls over cached memory when the user asks for real-time data.\n\n"
                    + "\n".join(tool_lines)
                )

        # List cached skills
        if self._skills_cache:
            skill_lines = [f"- **{s.name}**: {s.description}" for s in self._skills_cache]
            parts.append("## Available Skills\n\n" + "\n".join(skill_lines))

        # Runtime metadata
        now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
        metadata = (
            f"## Runtime Metadata\n\n"
            f"- **Date/Time**: {now}\n"
            f"- **Session**: {session_key}\n"
            f"- **Workspace**: {self._cwd}"
        )
        parts.append(metadata)

        return "\n\n---\n\n".join(parts)

    # -- Custom tool definitions --

    @staticmethod
    def _text_result(text: str) -> dict[str, Any]:
        """Format a plain-text string as an MCP tool result."""
        return {"content": [{"type": "text", "text": text}]}

    def _build_custom_tools(self) -> list:
        """Build the list of custom tool functions for the SDK agent.

        Returns decorated callables that the SDK will expose as tools:
          - send_message: Route a text message through the gateway callback
          - send_file: Route a file through the gateway callback
          - remember: Store a fact in long-term memory
          - recall: Search long-term memory for matching facts
          - stock_quote: (optional) Fetch stock price if yfinance is installed

        Each tool uses the claude_agent_sdk @tool(name, description, input_schema)
        decorator and receives a single ``args`` dict parameter.

        Note: ``runner = self`` captures this SDKRunner instance at build time.
        All tool closures are bound to this specific instance, so callbacks
        (e.g. _send_callback) reflect mutations made after construction.
        """
        tools: list = []

        memory_mgr = self._memory_mgr
        runner = self

        cfg_tools = getattr(runner._config, "tools", None)
        cfg_extra = getattr(cfg_tools, "extra", {}) if cfg_tools else {}
        groq_api_key = (
            cfg_extra.get("groq_api_key", "") if isinstance(cfg_extra, dict) else ""
        ) or os.environ.get("GROQ_API_KEY", "")
        groq_api_key = groq_api_key.strip()

        semantic_memory = None
        if groq_api_key:
            try:
                from grip.memory.semantic import SemanticMemory

                semantic_memory = SemanticMemory(groq_api_key=groq_api_key)
            except Exception as exc:
                logger.warning("Semantic memory unavailable at startup: {}", exc)

        @tool(
            "send_message",
            "Send a text message to the user via the configured channel.",
            {"text": str, "session_key": str},
        )
        async def send_message(args: dict[str, Any]) -> dict[str, Any]:
            cb = runner._send_callback
            if cb is None:
                return runner._text_result("Send callback not configured; message not delivered.")
            if inspect.iscoroutinefunction(cb):
                result = await cb(args["session_key"], args["text"])
            else:
                result = await asyncio.to_thread(cb, args["session_key"], args["text"])
            return runner._text_result(str(result) if result is not None else "Message sent.")

        @tool(
            "send_file",
            "Send a file to the user via the configured channel.",
            {"file_path": str, "caption": str, "session_key": str},
        )
        async def send_file(args: dict[str, Any]) -> dict[str, Any]:
            cb = runner._send_file_callback
            if cb is None:
                return runner._text_result("Send file callback not configured; file not delivered.")
            if inspect.iscoroutinefunction(cb):
                result = await cb(args["session_key"], args["file_path"], args["caption"])
            else:
                result = await asyncio.to_thread(cb, args["session_key"], args["file_path"], args["caption"])
            return runner._text_result(str(result) if result is not None else "File sent.")

        @tool(
            "remember",
            "Store a fact in long-term memory for future recall.",
            {"fact": str, "category": str},
        )
        async def remember(args: dict[str, Any]) -> dict[str, Any]:
            entry = f"- [{args['category']}] {args['fact']}"
            memory_mgr.append_to_memory(entry)

            # Best-effort semantic indexing; never break regular memory writes.
            if semantic_memory is not None:
                try:
                    semantic_memory.add(
                        text=args["fact"],
                        metadata={"category": args["category"], "source": "remember_tool"},
                    )
                except Exception as exc:
                    logger.warning("Failed to index semantic memory: {}", exc)

            return runner._text_result(f"Stored fact under category '{args['category']}'.")

        @tool(
            "recall",
            "Search long-term memory for facts matching the query.",
            {"query_text": str},
        )
        async def recall(args: dict[str, Any]) -> dict[str, Any]:
            results = memory_mgr.search_memory(args["query_text"], max_results=10)
            if not results:
                return runner._text_result("No matching facts found in memory.")
            return runner._text_result("\n".join(results))

        @tool(
            "semantic_recall",
            "Search semantically similar long-term memories. Falls back to keyword recall when unavailable.",
            {"query": str, "top_k": int},
        )
        async def semantic_recall(args: dict[str, Any]) -> dict[str, Any]:
            query = (args.get("query") or "").strip()
            if not query:
                return runner._text_result("Missing query.")
            try:
                top_k = max(1, min(int(args.get("top_k", 5)), 20))
            except Exception:
                top_k = 5

            if semantic_memory is None:
                fallback = memory_mgr.search_memory(query, max_results=top_k)
                if not fallback:
                    return runner._text_result("No matching facts found in memory.")
                return runner._text_result("[fallback: keyword recall]\n" + "\n".join(fallback))

            try:
                hits = semantic_memory.search(query=query, top_k=top_k)
                if not hits:
                    return runner._text_result("No semantically similar memories found.")
                lines = []
                for i, h in enumerate(hits, 1):
                    meta = h.get("metadata") or {}
                    cat = meta.get("category", "unknown")
                    dist = h.get("distance")
                    dist_txt = f"{float(dist):.4f}" if isinstance(dist, (float, int)) else "n/a"
                    lines.append(f"{i}. [{cat}] {h.get('text', '')} (distance: {dist_txt})")
                return runner._text_result("\n".join(lines))
            except Exception as exc:
                logger.warning("semantic_recall failed, using fallback: {}", exc)
                fallback = memory_mgr.search_memory(query, max_results=top_k)
                if not fallback:
                    return runner._text_result("No matching facts found in memory.")
                return runner._text_result("[fallback: keyword recall]\n" + "\n".join(fallback))

        @tool(
            "summarize_today",
            "Create and save today's daily memory archive.",
            {},
        )
        async def summarize_today(args: dict[str, Any]) -> dict[str, Any]:
            if not groq_api_key:
                return runner._text_result("Missing groq_api_key in tools.extra.groq_api_key")
            try:
                from grip.memory.archiver import create_daily_summary, today_iso

                day = today_iso()
                out_path = await asyncio.to_thread(create_daily_summary, day, groq_api_key)
                return runner._text_result(f"Daily archive created: {out_path}")
            except Exception as exc:
                return runner._text_result(f"Failed to create daily archive: {exc}")

        @tool(
            "summarize_month",
            "Create and save a monthly memory digest.",
            {"year": int, "month": int},
        )
        async def summarize_month(args: dict[str, Any]) -> dict[str, Any]:
            if not groq_api_key:
                return runner._text_result("Missing groq_api_key in tools.extra.groq_api_key")
            try:
                from grip.memory.archiver import create_monthly_summary, previous_month

                year = args.get("year")
                month = args.get("month")
                if year is None or month is None:
                    year, month = previous_month()
                out_path = await asyncio.to_thread(create_monthly_summary, int(year), int(month), groq_api_key)
                return runner._text_result(f"Monthly archive created: {out_path}")
            except Exception as exc:
                return runner._text_result(f"Failed to create monthly archive: {exc}")

        @tool(
            "list_memory_archives",
            "List available daily and monthly memory archives.",
            {},
        )
        async def list_memory_archives(args: dict[str, Any]) -> dict[str, Any]:
            try:
                from grip.memory.archiver import list_archives

                archives = await asyncio.to_thread(list_archives)
                daily = archives.get("daily", [])
                monthly = archives.get("monthly", [])
                return runner._text_result(
                    "Daily archives:\n"
                    + ("\n".join(daily) if daily else "(none)")
                    + "\n\nMonthly archives:\n"
                    + ("\n".join(monthly) if monthly else "(none)")
                )
            except Exception as exc:
                return runner._text_result(f"Failed to list archives: {exc}")

        @tool(
            "read_memory_archive",
            "Read a memory archive by date (YYYY-MM-DD daily or YYYY-MM monthly).",
            {"date": str},
        )
        async def read_memory_archive(args: dict[str, Any]) -> dict[str, Any]:
            ident = (args.get("date") or "").strip()
            if not ident:
                return runner._text_result("Missing date.")
            try:
                from grip.memory.archiver import read_archive

                text = await asyncio.to_thread(read_archive, ident)
                return runner._text_result(text)
            except Exception as exc:
                return runner._text_result(f"Failed to read archive: {exc}")

        tools.extend([
            send_message,
            send_file,
            remember,
            recall,
            semantic_recall,
            summarize_today,
            summarize_month,
            list_memory_archives,
            read_memory_archive,
        ])

        # Web search plus tool (native module-backed multi-provider routing)
        runner_ref = runner

        @tool(
            "web_search",
            "Search the web with smart provider routing (Serper, Tavily, Perplexity, Exa). Returns titles, URLs, and snippets.",
            {"query": str, "max_results": int},
        )
        async def web_search(args: dict[str, Any]) -> dict[str, Any]:
            from grip.tools.web_search_plus import search_web_plus

            query = (args.get("query") or "").strip()
            if not query:
                return runner_ref._text_result("Missing query.")
            max_results = min(max(int(args.get("max_results", 5)), 1), 10)

            cfg_tools = getattr(runner_ref._config, "tools", None)
            cfg_extra = getattr(cfg_tools, "extra", {}) if cfg_tools else {}

            provider, rendered, errors = await search_web_plus(
                query,
                max_results=max_results,
                extra=cfg_extra if isinstance(cfg_extra, dict) else {},
            )
            if rendered:
                return runner_ref._text_result(rendered)

            if errors:
                return runner_ref._text_result(
                    "Web search unavailable. Tried providers with errors: " + ", ".join(errors)
                )
            return runner_ref._text_result("Web search unavailable.")

        tools.append(web_search)

        @tool(
            "get_weather",
            "Get current weather and forecast for a location via wttr.in.",
            {"location": str, "days": int},
        )
        async def get_weather(args: dict[str, Any]) -> dict[str, Any]:
            import requests

            location = (args.get("location") or "").strip()
            if not location:
                return runner._text_result("Missing location.")

            try:
                days = int(args.get("days", 1))
            except Exception:
                days = 1
            days = max(1, min(days, 5))

            def _fetch_weather() -> dict[str, Any]:
                resp = requests.get(f"https://wttr.in/{location}?format=j1", timeout=10)
                resp.raise_for_status()
                return resp.json()

            try:
                data = await asyncio.to_thread(_fetch_weather)
            except Exception as exc:
                return runner._text_result(f"Failed to fetch weather: {exc}")

            current = (data.get("current_condition") or [{}])[0]
            cond = ((current.get("weatherDesc") or [{}])[0] or {}).get("value", "Unknown")
            lines = [
                f"Weather for {location}",
                f"Current: {current.get('temp_C', '?')}°C, {cond}, humidity {current.get('humidity', '?')}%, wind {current.get('windspeedKmph', '?')} km/h {current.get('winddir16Point', '?')}",
                "",
                f"Forecast ({days} day{'s' if days != 1 else ''}):",
            ]

            for day in (data.get("weather") or [])[:days]:
                date = day.get("date", "?")
                desc = "Unknown"
                hourly = day.get("hourly") or []
                if hourly:
                    desc = ((hourly[0].get("weatherDesc") or [{}])[0] or {}).get("value", "Unknown")
                lines.append(f"- {date}: {day.get('mintempC', '?')}°C to {day.get('maxtempC', '?')}°C, {desc}")

            return runner._text_result("\\n".join(lines))

        tools.append(get_weather)

        @tool(
            "youtube_transcript",
            "Fetch transcript/captions from a YouTube video via Apify.",
            {"video_url": str},
        )
        async def youtube_transcript(args: dict[str, Any]) -> dict[str, Any]:
            import requests

            video_url = (args.get("video_url") or "").strip()
            if not video_url:
                return runner._text_result("Missing video_url.")

            cfg_tools = getattr(runner._config, "tools", None)
            cfg_extra = getattr(cfg_tools, "extra", {}) if cfg_tools else {}
            token = (cfg_extra.get("apify_api_token", "") if isinstance(cfg_extra, dict) else "") or os.environ.get("APIFY_API_TOKEN", "")
            token = token.strip()
            if not token:
                return runner._text_result("Missing Apify token. Set tools.extra.apify_api_token or APIFY_API_TOKEN.")

            def _extract_video_id(url: str) -> str | None:
                if re.fullmatch(r"[a-zA-Z0-9_-]{11}", url):
                    return url
                parsed = urlparse(url)
                if "youtu.be" in parsed.netloc:
                    return parsed.path.lstrip("/").split("?")[0][:11] or None
                q = parse_qs(parsed.query)
                if q.get("v"):
                    return q["v"][0]
                m = re.search(r"/(?:embed|shorts|v)/([a-zA-Z0-9_-]{11})", parsed.path)
                return m.group(1) if m else None

            video_id = _extract_video_id(video_url)
            if not video_id:
                return runner._text_result("Could not extract YouTube video ID from URL.")

            cache_dir = Path.home() / ".grip" / "cache" / "youtube"
            cache_dir.mkdir(parents=True, exist_ok=True)
            cache_file = cache_dir / f"{video_id}.json"
            if cache_file.exists():
                try:
                    cached = json.loads(cache_file.read_text(encoding="utf-8"))
                    transcript = (cached.get("transcript") or "").strip()
                    if transcript:
                        return runner._text_result(f"[cached] Transcript for {video_id}:\\n\\n{transcript}")
                except Exception:
                    pass

            actor_id = "bernardo_apartado~youtube-transcript-downloader"
            base = "https://api.apify.com/v2"

            def _run() -> str:
                headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
                run = requests.post(f"{base}/acts/{actor_id}/runs", headers=headers, json={"startUrls": [{"url": video_url}]}, timeout=30)
                run.raise_for_status()
                run_id = run.json()["data"]["id"]

                status_data = None
                deadline = time.time() + 180
                while time.time() < deadline:
                    st = requests.get(f"{base}/actor-runs/{run_id}", headers={"Authorization": f"Bearer {token}"}, timeout=15)
                    st.raise_for_status()
                    status_data = st.json().get("data", {})
                    status = status_data.get("status")
                    if status == "SUCCEEDED":
                        break
                    if status in {"FAILED", "ABORTED", "TIMED-OUT"}:
                        raise RuntimeError(f"Apify actor status: {status}")
                    time.sleep(3)
                else:
                    raise TimeoutError("Timeout waiting for Apify actor")

                ds = status_data.get("defaultDatasetId")
                if not ds:
                    return ""
                items = requests.get(f"{base}/datasets/{ds}/items", headers={"Authorization": f"Bearer {token}"}, timeout=30)
                items.raise_for_status()
                data = items.json()
                if not isinstance(data, list):
                    return ""
                parts: list[str] = []
                for it in data:
                    if not isinstance(it, dict):
                        continue
                    txt = (it.get("text") or "").strip()
                    if txt:
                        parts.append(txt)
                    for c in it.get("captions") or []:
                        if isinstance(c, dict) and c.get("text"):
                            parts.append(str(c["text"]).strip())
                return " ".join(p for p in parts if p).strip()

            try:
                transcript = await asyncio.to_thread(_run)
            except Exception as exc:
                return runner._text_result(f"Failed to fetch transcript: {exc}")
            if not transcript:
                return runner._text_result("No transcript text returned.")

            try:
                cache_file.write_text(json.dumps({"video_id": video_id, "video_url": video_url, "transcript": transcript}, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception:
                pass

            return runner._text_result(transcript)

        tools.append(youtube_transcript)

        @tool(
            "twitter_search",
            "Search X/Twitter for tweets via Apify (query, @username, or profile URL).",
            {"query": str, "max_results": int},
        )
        async def twitter_search(args: dict[str, Any]) -> dict[str, Any]:
            import requests

            query = (args.get("query") or "").strip()
            if not query:
                return runner._text_result("Missing query.")
            try:
                max_results = int(args.get("max_results", 10))
            except Exception:
                max_results = 10
            max_results = max(1, min(max_results, 50))

            cfg_tools = getattr(runner._config, "tools", None)
            cfg_extra = getattr(cfg_tools, "extra", {}) if cfg_tools else {}
            token = (cfg_extra.get("apify_api_token", "") if isinstance(cfg_extra, dict) else "") or os.environ.get("APIFY_API_TOKEN", "")
            token = token.strip()
            if not token:
                return runner._text_result("Missing Apify token. Set tools.extra.apify_api_token or APIFY_API_TOKEN.")

            cache_dir = Path.home() / ".grip" / "cache" / "twitter"
            cache_dir.mkdir(parents=True, exist_ok=True)
            cache_key = re.sub(r"[^a-zA-Z0-9._-]+", "_", query.lower())[:120]
            cache_file = cache_dir / f"{cache_key}_{max_results}.json"
            if cache_file.exists():
                try:
                    cached = json.loads(cache_file.read_text(encoding="utf-8"))
                    if cached.get("rendered"):
                        return runner._text_result("[cached]\\n" + cached["rendered"])
                except Exception:
                    pass

            actor_id = "CJdippxWmn9uRfooo"
            base = "https://api.apify.com/v2"
            q = query.strip()
            if q.startswith("@") and len(q) > 1:
                input_data = {"startUrls": [{"url": f"https://x.com/{q.lstrip('@')}"}], "maxItems": max_results}
            elif q.startswith("https://x.com/") or q.startswith("https://twitter.com/"):
                input_data = {"startUrls": [{"url": q}], "maxItems": max_results}
            else:
                input_data = {"searchTerms": [q], "maxItems": max_results}

            def _run() -> list[dict[str, Any]]:
                headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
                run = requests.post(f"{base}/acts/{actor_id}/runs", headers=headers, json=input_data, timeout=30)
                run.raise_for_status()
                run_id = run.json()["data"]["id"]

                status_data = None
                deadline = time.time() + 180
                while time.time() < deadline:
                    st = requests.get(f"{base}/actor-runs/{run_id}", headers={"Authorization": f"Bearer {token}"}, timeout=15)
                    st.raise_for_status()
                    status_data = st.json().get("data", {})
                    status = status_data.get("status")
                    if status == "SUCCEEDED":
                        break
                    if status in {"FAILED", "ABORTED", "TIMED-OUT"}:
                        raise RuntimeError(f"Apify actor status: {status}")
                    time.sleep(3)
                else:
                    raise TimeoutError("Timeout waiting for Apify actor")

                ds = status_data.get("defaultDatasetId")
                if not ds:
                    return []
                items = requests.get(f"{base}/datasets/{ds}/items", headers={"Authorization": f"Bearer {token}"}, timeout=30)
                items.raise_for_status()
                payload = items.json()
                return payload if isinstance(payload, list) else []

            try:
                items = await asyncio.to_thread(_run)
            except Exception as exc:
                return runner._text_result(f"Failed to fetch tweets: {exc}")
            if not items:
                return runner._text_result("No tweets returned.")

            lines = [f"X/Twitter results for: {query}", f"Count: {len(items)}", ""]
            for item in items[:max_results]:
                author_obj = item.get("author") or item.get("user") or {}
                author = author_obj.get("userName") or author_obj.get("screen_name") or "unknown"
                text_val = (item.get("text") or item.get("full_text") or "").strip()
                likes = item.get("likeCount", item.get("favorite_count", 0))
                rts = item.get("retweetCount", item.get("retweet_count", 0))
                replies = item.get("replyCount", item.get("conversation_count", 0))
                tw_id = str(item.get("id") or item.get("id_str") or "")
                url = item.get("url") or item.get("twitterUrl") or (f"https://x.com/{author}/status/{tw_id}" if tw_id and author != "unknown" else "")
                lines.extend([
                    f"@{author}: {text_val}",
                    f"Likes {likes} | RTs {rts} | Replies {replies}",
                    url,
                    "",
                ])

            rendered = "\\n".join(lines).strip()
            try:
                cache_file.write_text(json.dumps({"query": query, "max_results": max_results, "rendered": rendered, "count": len(items)}, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception:
                pass
            return runner._text_result(rendered)

        tools.append(twitter_search)

        try:
            import yfinance  # noqa: F401

            @tool(
                "stock_quote",
                "Fetch the current stock price for a given ticker symbol.",
                {"symbol": str},
            )
            async def stock_quote(args: dict[str, Any]) -> dict[str, Any]:
                import yfinance as yf

                def _fetch_quote(symbol: str) -> dict:
                    ticker = yf.Ticker(symbol)
                    return ticker.info

                info = await asyncio.to_thread(_fetch_quote, args["symbol"])
                price = info.get("currentPrice") or info.get("regularMarketPrice", "N/A")
                name = info.get("shortName", args["symbol"])
                return runner._text_result(f"{name} ({args['symbol']}): ${price}")

            tools.append(stock_quote)
        except ImportError:
            pass

        return tools

    def _build_subagent_hooks(self, session_key: str) -> dict[str, Any]:
        """Build SDK hooks for subagent start/stop observability only."""

        memory_mgr = self._memory_mgr

        def _extract_nested(mapping: Mapping[str, Any], *paths: tuple[str, ...]) -> Any:
            for path in paths:
                cur: Any = mapping
                ok = True
                for key in path:
                    if isinstance(cur, Mapping) and key in cur:
                        cur = cur[key]
                    else:
                        ok = False
                        break
                if ok and cur is not None:
                    return cur
            return None

        async def on_subagent_start(input_data, tool_use_id, context) -> dict[str, Any]:
            prompt = _extract_nested(input_data, ("prompt",), ("user_prompt",), ("input", "prompt"))
            model = _extract_nested(input_data, ("model",), ("agent_model",), ("input", "model"))
            depth = _extract_nested(input_data, ("depth",), ("subagent_depth",), ("input", "depth"))
            agent_id = input_data.get("agent_id", "")
            agent_type = input_data.get("agent_type", "")

            logger.info(
                "SubagentStart: session={} agent_id={} type={} model={} depth={} prompt={}",
                input_data.get("session_id", ""),
                agent_id,
                agent_type,
                model or "unknown",
                depth if depth is not None else "unknown",
                (str(prompt)[:300] if prompt else ""),
            )
            if memory_mgr:
                memory_mgr.append_history(
                    "[SubagentStart] ({}) id={} type={} model={} depth={} prompt={}".format(
                        session_key,
                        agent_id,
                        agent_type,
                        model or "unknown",
                        depth if depth is not None else "unknown",
                        str(prompt)[:180] if prompt else "",
                    )
                )
            return {}

        async def on_subagent_stop(input_data, tool_use_id, context) -> dict[str, Any]:
            result_summary = _extract_nested(input_data, ("result_summary",), ("result",), ("output",), ("input", "result"))
            usage = _extract_nested(input_data, ("usage",), ("token_usage",), ("input", "usage"))
            tokens_used = _extract_nested(
                input_data,
                ("total_tokens",),
                ("tokens_used",),
                ("usage", "total_tokens"),
                ("usage", "totalTokens"),
                ("usage", "output_tokens"),
            )
            agent_id = input_data.get("agent_id", "")
            agent_type = input_data.get("agent_type", "")

            logger.info(
                "SubagentStop: session={} agent_id={} type={} tokens={} summary={} usage={}",
                input_data.get("session_id", ""),
                agent_id,
                agent_type,
                tokens_used if tokens_used is not None else "unknown",
                (str(result_summary)[:300] if result_summary else ""),
                str(usage)[:300] if usage else "",
            )
            if memory_mgr:
                memory_mgr.append_history(
                    "[SubagentStop] ({}) id={} type={} tokens={} summary={}".format(
                        session_key,
                        agent_id,
                        agent_type,
                        tokens_used if tokens_used is not None else "unknown",
                        str(result_summary)[:180] if result_summary else "",
                    )
                )
            return {}

        return {
            "SubagentStart": [HookMatcher(hooks=[on_subagent_start])],
            "SubagentStop": [HookMatcher(hooks=[on_subagent_stop])],
        }

    # -- EngineProtocol implementation --

    async def run(
        self,
        user_message: str,
        *,
        session_key: str = "cli:default",
        model: str | None = None,
    ) -> AgentRunResult:
        """Send a user message through the Claude Agent SDK and return the result.

        Uses ClaudeSDKClient to run the agent loop, collecting the final result
        and tool call names. Persists the exchange to history via MemoryManager.
        """
        custom_tools = self._custom_tools
        system_prompt = self._build_system_prompt(user_message, session_key, custom_tools)
        mcp_config = self._mcp_config
        allowed_tools = list(self._allowed_tools_base)

        effective_model = model or self._model

        # Wrap custom tools in an in-process MCP server (the SDK expects
        # ClaudeAgentOptions.tools to be a list of built-in tool name strings,
        # NOT SdkMcpTool objects).
        grip_server = create_sdk_mcp_server(
            "grip_tools", version="1.0.0", tools=custom_tools
        )
        mcp_servers: dict[str, Any] = {srv["name"]: srv for srv in mcp_config}
        mcp_servers["grip_tools"] = grip_server

        # Ensure the custom tool names are in allowed_tools so the SDK permits
        # them.  SDK MCP tools follow the mcp__<server_key>__<tool> convention.
        custom_tool_names = [f"mcp__grip_tools__{t.name}" for t in custom_tools]
        allowed_tools.extend(custom_tool_names)

        # Adaptive thinking via --effort flag (optional, model default if not set)
        extra_args: dict[str, str | None] = {}
        sdk_effort = self._config.agents.defaults.sdk_effort
        if sdk_effort:
            extra_args["effort"] = sdk_effort

        env_opts: dict[str, str] = {
            # Prevent the Claude CLI from refusing to start when grip is
            # invoked from inside a Claude Code session (nested session guard).
            "CLAUDECODE": "",
        }
        if self._api_key:
            env_opts["ANTHROPIC_API_KEY"] = self._api_key
        tool_search = self._config.tools.enable_tool_search
        if tool_search and tool_search != "auto":
            env_opts["ENABLE_TOOL_SEARCH"] = tool_search

        # Adaptive thinking via --effort flag
        extra_args: dict[str, str | None] = {}
        sdk_effort = self._config.agents.defaults.sdk_effort
        if sdk_effort:
            extra_args["effort"] = sdk_effort


        # Do not restrict built-in SDK tools; keep all native tools available.
        # This includes: Read, Write, Edit, Bash, Glob, Grep, WebSearch, WebFetch, AskUserQuestion.
        native_tools: list[str] | None = None
        # Allow all SDK built-ins + MCP tools by default (no unnecessary restriction).
        # This keeps native tools (Read/Write/Edit/Bash/Glob/Grep/WebSearch/WebFetch/AskUserQuestion)
        # available while still exposing custom grip MCP tools.
        final_allowed_tools = None

        options = ClaudeAgentOptions(
            model=effective_model,
            system_prompt=system_prompt,
            mcp_servers=mcp_servers,
            permission_mode=self._permission_mode,
            tools=native_tools,
            cwd=self._cwd,
            allowed_tools=final_allowed_tools,
            env=env_opts if env_opts else None,
            extra_args=extra_args if extra_args else {},
            hooks=self._build_subagent_hooks(session_key),
        )

        tool_calls_made: list[str] = []
        result_text: str | None = None
        thinking_parts: list[str] = []

        try:
            # ClaudeSDKClient supports custom tools (via @tool / SDK MCP
            # servers), hooks, and multi-turn conversations.  The simpler
            # query() function does NOT support custom tools.
            async with ClaudeSDKClient(options=options) as client:
                await client.query(user_message)
                async for message in client.receive_response():
                    if isinstance(message, AssistantMessage):
                        for block in getattr(message, "content", []):
                            if hasattr(block, "name"):
                                tool_calls_made.append(block.name)
                            # Capture thinking blocks
                            block_type = getattr(block, "type", None)
                            if block_type == "thinking":
                                thinking_text = getattr(block, "thinking", None)
                                if thinking_text:
                                    thinking_parts.append(thinking_text)
                    # ResultMessage is the authoritative final response;
                    # AssistantMessage text blocks duplicate it, so text
                    # is only captured here to avoid printing twice.
                    elif isinstance(message, ResultMessage) and getattr(message, "result", None):
                        result_text = message.result
        except ExceptionGroup as eg:
            # The SDK may raise CLIConnectionError wrapped in an ExceptionGroup
            # during query cleanup when the CLI subprocess exits before the
            # transport finishes writing a control response. This is a known
            # race condition — the response was already collected above.
            _cli_errors, rest = eg.split(CLIConnectionError)
            if rest:
                raise rest from eg
            logger.debug("Suppressed CLIConnectionError during query cleanup: {}", eg)

        # Prepend thinking blocks if present and enabled in config
        show_thinking = getattr(self._config.agents.defaults, "sdk_show_thinking", True)
        response_text = result_text or ""
        if thinking_parts and show_thinking:
            thinking_combined = "\n\n---\n\n".join(thinking_parts)
            response_text = f"💭 *Thinking...*\n\n{thinking_combined}\n\n──────────\n\n{response_text}"

        # Persist user message and agent response to conversation history
        self._memory_mgr.append_history(f"User ({session_key}): {user_message[:200]}")
        self._memory_mgr.append_history(f"Agent ({session_key}): {response_text[:200]}")

        return AgentRunResult(
            response=response_text,
            tool_calls_made=tool_calls_made,
        )

    async def consolidate_session(self, session_key: str) -> None:
        """No-op for SDK engine: the SDK manages its own context window internally.

        Logged for observability so operators can see when consolidation was requested.
        """
        logger.info(
            "consolidate_session called for '{}' (SDK handles context internally)", session_key
        )

    async def reset_session(self, session_key: str) -> None:
        """Clear all state for a session and delete persisted session."""
        self._session_mgr.delete(session_key)
        logger.info("Reset session '{}'", session_key)
