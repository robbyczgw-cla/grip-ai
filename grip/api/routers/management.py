"""Management endpoints for the grip REST API.

Provides read-only access to system status, masked config, cron jobs,
skills, and memory. Write operations are limited to cron CRUD and
cron enable/disable toggles.

Deliberately NOT exposed: config mutation, skill installation, hooks
management — all too dangerous for remote HTTP access.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from grip.api.auth import require_auth
from grip.api.dependencies import (
    check_rate_limit,
    check_token_rate_limit,
    get_config,
    get_memory_mgr,
)
from grip.config.schema import ChannelsConfig, GripConfig
from grip.memory.manager import MemoryManager
from grip.memory.semantic import SemanticMemory

router = APIRouter(prefix="/api/v1", tags=["management"])


def _memory_archives_dir() -> Path:
    return Path.home() / ".grip" / "memory"


def _safe_archive_count(directory: Path) -> int:
    try:
        if not directory.exists():
            return 0
        return len([p for p in directory.iterdir() if p.is_file()])
    except Exception:
        return 0


def _latest_daily_archive(daily_dir: Path) -> str | None:
    try:
        if not daily_dir.exists():
            return None
        daily_files = [p for p in daily_dir.iterdir() if p.is_file()]
        if not daily_files:
            return None
        latest = max(daily_files, key=lambda f: f.stat().st_mtime)
        return latest.stem
    except Exception:
        return None


def _read_archive_file(archive_type: str, date: str) -> tuple[Path, str]:
    if archive_type not in {"daily", "monthly"}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="type must be daily or monthly")

    cleaned = date.strip()
    if not cleaned:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="date is required")
    if "/" in cleaned or ".." in cleaned or "\\" in cleaned:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid date")

    base = _memory_archives_dir() / archive_type
    target = base / f"{cleaned}.md"
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="archive not found")

    try:
        content = target.read_text(encoding="utf-8")
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"failed to read archive: {exc}") from exc

    return target, content


# ── Status ──


@router.get(
    "/status",
    dependencies=[Depends(check_rate_limit)],
)
async def get_status(
    request: Request,
    token: str = Depends(require_auth),
    config: GripConfig = Depends(get_config),  # noqa: B008
) -> dict:
    """System status — same data as `grip status` CLI."""
    check_token_rate_limit(request, token)

    defaults = config.agents.defaults
    ws_path = defaults.workspace.expanduser().resolve()

    sessions_dir = ws_path / "sessions"

    def _count_sessions() -> int:
        if sessions_dir.exists():
            return len(list(sessions_dir.glob("*.json")))
        return 0

    session_count = await asyncio.to_thread(_count_sessions)

    channels_status = {}
    for name in ChannelsConfig.CHANNEL_NAMES:
        ch = getattr(config.channels, name, None)
        if ch and ch.is_active():
            channels_status[name] = "active"
        elif ch and ch.enabled:
            channels_status[name] = "no_token"
        else:
            channels_status[name] = "disabled"

    return {
        "model": defaults.model,
        "max_tokens": defaults.max_tokens,
        "temperature": defaults.temperature,
        "max_tool_iterations": defaults.max_tool_iterations,
        "workspace": str(ws_path),
        "session_count": session_count,
        "sandbox_enabled": config.tools.restrict_to_workspace,
        "shell_timeout": config.tools.shell_timeout,
        "mcp_server_count": len(config.tools.mcp_servers),
        "channels": channels_status,
        "heartbeat_enabled": config.heartbeat.enabled,
        "heartbeat_interval_minutes": config.heartbeat.interval_minutes,
        "tool_execute_enabled": config.gateway.api.enable_tool_execute,
    }


# ── Config (masked) ──


def _mask_secrets(obj: Any) -> Any:
    """Recursively mask strings that look like API keys or tokens.

    Replicates the logic from grip/cli/config_cmd.py:_mask_secrets()
    so the API returns the same masked output as `grip config show`.
    """
    import re

    if isinstance(obj, str):
        if len(obj) > 8 and any(
            kw in obj.lower() for kw in ("sk-", "key-", "token", "secret", "grip_")
        ):
            return obj[:4] + "***" + obj[-4:]
        if len(obj) > 20 and re.match(r"^[A-Za-z0-9_\-]+$", obj):
            return obj[:4] + "***" + obj[-4:]
        return obj
    elif isinstance(obj, dict):
        return {k: _mask_secrets(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_mask_secrets(item) for item in obj]
    return obj


def _stringify_paths(obj: dict) -> None:
    """Convert Path values to strings recursively."""
    for key, value in obj.items():
        if isinstance(value, Path):
            obj[key] = str(value)
        elif isinstance(value, dict):
            _stringify_paths(value)


@router.get(
    "/config",
    dependencies=[Depends(check_rate_limit)],
)
async def get_config_masked(
    request: Request,
    token: str = Depends(require_auth),
    config: GripConfig = Depends(get_config),  # noqa: B008
) -> dict:
    """Return the full config with all secrets masked."""
    check_token_rate_limit(request, token)

    data = config.model_dump(mode="json")
    _stringify_paths(data)
    masked = _mask_secrets(data)
    return {"config": masked}


# ── Cron ──


class CronJobCreateRequest(BaseModel):
    """Request body for creating a cron job."""

    name: str = Field(..., min_length=1, max_length=128)
    schedule: str = Field(..., min_length=5, max_length=128)
    prompt: str = Field(..., min_length=1, max_length=10000)
    reply_to: str = Field(default="", max_length=256)


@router.get(
    "/cron",
    dependencies=[Depends(check_rate_limit)],
)
async def list_cron_jobs(
    request: Request,
    token: str = Depends(require_auth),
) -> dict:
    """List all configured cron jobs."""
    check_token_rate_limit(request, token)

    cron_svc = getattr(request.app.state, "cron_service", None)
    if cron_svc is None:
        return {"jobs": [], "count": 0}

    jobs = cron_svc.list_jobs()
    return {
        "jobs": [job.to_dict() for job in jobs],
        "count": len(jobs),
    }


@router.post(
    "/cron",
    dependencies=[Depends(check_rate_limit)],
    status_code=status.HTTP_201_CREATED,
)
async def create_cron_job(
    body: CronJobCreateRequest,
    request: Request,
    token: str = Depends(require_auth),
) -> dict:
    """Create a new cron job."""
    check_token_rate_limit(request, token)

    cron_svc = getattr(request.app.state, "cron_service", None)
    if cron_svc is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Cron service not available",
        )

    job = cron_svc.add_job(body.name, body.schedule, body.prompt, reply_to=body.reply_to)
    return {"created": True, "job": job.to_dict()}


@router.delete(
    "/cron/{job_id}",
    dependencies=[Depends(check_rate_limit)],
)
async def delete_cron_job(
    job_id: str,
    request: Request,
    token: str = Depends(require_auth),
) -> dict:
    """Delete a cron job by ID."""
    check_token_rate_limit(request, token)

    cron_svc = getattr(request.app.state, "cron_service", None)
    if cron_svc is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Cron service not available",
        )

    if not cron_svc.remove_job(job_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Cron job not found",
        )
    return {"deleted": True, "job_id": job_id}


@router.post(
    "/cron/{job_id}/enable",
    dependencies=[Depends(check_rate_limit)],
)
async def enable_cron_job(
    job_id: str,
    request: Request,
    token: str = Depends(require_auth),
) -> dict:
    """Enable a cron job."""
    check_token_rate_limit(request, token)

    cron_svc = getattr(request.app.state, "cron_service", None)
    if cron_svc is None or not cron_svc.enable_job(job_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Cron job not found",
        )
    return {"enabled": True, "job_id": job_id}


@router.post(
    "/cron/{job_id}/disable",
    dependencies=[Depends(check_rate_limit)],
)
async def disable_cron_job(
    job_id: str,
    request: Request,
    token: str = Depends(require_auth),
) -> dict:
    """Disable a cron job."""
    check_token_rate_limit(request, token)

    cron_svc = getattr(request.app.state, "cron_service", None)
    if cron_svc is None or not cron_svc.disable_job(job_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Cron job not found",
        )
    return {"disabled": True, "job_id": job_id}


# ── Skills ──


@router.get(
    "/skills",
    dependencies=[Depends(check_rate_limit)],
)
async def list_skills(
    request: Request,
    token: str = Depends(require_auth),
) -> dict:
    """List all loaded agent skills."""
    check_token_rate_limit(request, token)

    skills_loader = getattr(request.app.state, "skills_loader", None)
    if skills_loader is None:
        return {"skills": [], "count": 0}

    skills = skills_loader.list_skills()
    return {
        "skills": [
            {
                "name": s.name,
                "description": s.description,
                "always_loaded": s.always_loaded,
                "source": str(s.source_path),
            }
            for s in skills
        ],
        "count": len(skills),
    }


# ── Memory ──


@router.get(
    "/memory",
    dependencies=[Depends(check_rate_limit)],
)
async def get_memory(
    request: Request,
    token: str = Depends(require_auth),
    memory_mgr: MemoryManager = Depends(get_memory_mgr),  # noqa: B008
) -> dict:
    """Read the contents of MEMORY.md."""
    check_token_rate_limit(request, token)

    content = await asyncio.to_thread(memory_mgr.read_memory)
    return {"content": content, "length": len(content)}


@router.get(
    "/memory/search",
    dependencies=[Depends(check_rate_limit)],
)
async def search_memory(
    q: str,
    request: Request,
    token: str = Depends(require_auth),
    memory_mgr: MemoryManager = Depends(get_memory_mgr),  # noqa: B008
) -> dict:
    """Search HISTORY.md for lines matching the query."""
    check_token_rate_limit(request, token)

    if not q or len(q) > 500:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Query must be 1-500 characters",
        )

    results = await asyncio.to_thread(memory_mgr.search_history, q)
    return {"query": q, "results": results, "count": len(results)}


@router.get(
    "/memory/archives",
    dependencies=[Depends(check_rate_limit)],
)
async def list_memory_archives(
    type: str,
    request: Request,
    token: str = Depends(require_auth),
) -> dict:
    """List daily or monthly memory archive files."""
    check_token_rate_limit(request, token)

    archive_type = (type or "").strip().lower()
    if archive_type not in {"daily", "monthly"}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="type must be daily or monthly")

    archive_dir = _memory_archives_dir() / archive_type
    files: list[dict[str, Any]] = []
    if archive_dir.exists():
        try:
            for file_path in sorted(
                [p for p in archive_dir.iterdir() if p.is_file() and p.suffix.lower() in {".md", ".txt"}],
                key=lambda p: p.name,
                reverse=True,
            ):
                try:
                    stat = file_path.stat()
                    files.append(
                        {
                            "name": file_path.name,
                            "date": file_path.stem,
                            "size": stat.st_size,
                            "updated_at": stat.st_mtime,
                        }
                    )
                except Exception:
                    continue
        except Exception:
            pass

    return {"type": archive_type, "archives": files, "count": len(files)}


@router.get(
    "/memory/archive",
    dependencies=[Depends(check_rate_limit)],
)
async def get_memory_archive(
    date: str,
    request: Request,
    token: str = Depends(require_auth),
    type: str = "daily",
) -> dict:
    """Get a specific memory archive by date (YYYY-MM-DD for daily, YYYY-MM for monthly)."""
    check_token_rate_limit(request, token)

    archive_type = (type or "daily").strip().lower()
    target, content = _read_archive_file(archive_type, date)

    return {
        "type": archive_type,
        "date": date,
        "path": str(target),
        "content": content,
        "length": len(content),
    }


# ── Metrics ──


@router.get(
    "/metrics",
    dependencies=[Depends(check_rate_limit)],
)
async def get_metrics(
    request: Request,
    token: str = Depends(require_auth),
) -> dict:
    """Return in-memory metrics snapshot."""
    check_token_rate_limit(request, token)

    from grip.observe.metrics import get_metrics as _get_metrics

    metrics = _get_metrics()
    return {"metrics": metrics.snapshot().to_dict()}


# ── Workflows ──


@router.get(
    "/workflows",
    dependencies=[Depends(check_rate_limit)],
)
async def list_workflows(
    request: Request,
    token: str = Depends(require_auth),
) -> dict:
    """List all saved workflow definitions."""
    check_token_rate_limit(request, token)

    store = _get_workflow_store(request)
    if store is None:
        return {"workflows": [], "count": 0}

    names = store.list_workflows()
    workflows = []
    for name in names:
        wf = store.load(name)
        if wf:
            workflows.append(
                {
                    "name": wf.name,
                    "description": wf.description,
                    "step_count": len(wf.steps),
                }
            )
    return {"workflows": workflows, "count": len(workflows)}


@router.get(
    "/workflows/{name}",
    dependencies=[Depends(check_rate_limit)],
)
async def get_workflow(
    name: str,
    request: Request,
    token: str = Depends(require_auth),
) -> dict:
    """Get a workflow definition by name."""
    check_token_rate_limit(request, token)

    store = _get_workflow_store(request)
    if store is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workflow not found")

    wf = store.load(name)
    if not wf:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workflow not found")

    return {"workflow": wf.to_dict()}


def _get_workflow_store(request: Request):
    """Get the workflow store from app.state, or create one from workspace."""
    store = getattr(request.app.state, "workflow_store", None)
    if store is not None:
        return store

    workspace = getattr(request.app.state, "workspace", None)
    if workspace is None:
        return None

    from grip.workflow.store import WorkflowStore

    return WorkflowStore(workspace.root / "workflows")
# ── Info (comprehensive dashboard data) ──


@router.get(
    "/info",
    dependencies=[Depends(check_rate_limit)],
)
async def get_info(
    request: Request,
    token: str = Depends(require_auth),
    config: GripConfig = Depends(get_config),  # noqa: B008
    memory_mgr: MemoryManager = Depends(get_memory_mgr),  # noqa: B008
) -> dict:
    """Comprehensive system info for /info command and dashboard."""
    import time

    check_token_rate_limit(request, token)

    defaults = config.agents.defaults
    ws_path = defaults.workspace.expanduser().resolve()

    # Model + effort
    model = defaults.model
    effort = getattr(defaults, "sdk_effort", "default")

    # Uptime + version
    from grip import __version__
    start_time: float = request.app.state.start_time
    uptime_seconds = time.time() - start_time

    # Memory info
    memory_content = await asyncio.to_thread(memory_mgr.read_memory)
    memory_size = len(memory_content.encode("utf-8"))

    # History count
    history_path = ws_path / "memory" / "HISTORY.md"
    history_count = 0
    if history_path.exists():
        try:
            history_text = history_path.read_text()
            history_count = history_text.count("\n## ")
            if history_count == 0:
                history_count = len([l for l in history_text.splitlines() if l.strip()])
        except Exception:
            pass

    # Cron jobs
    cron_svc = getattr(request.app.state, "cron_service", None)
    cron_jobs = []
    if cron_svc:
        jobs = cron_svc.list_jobs()
        cron_jobs = [j.to_dict() for j in jobs]

    # Tools/Skills
    tool_definitions = []
    try:
        registry = getattr(request.app.state, "tool_registry", None)
        if registry:
            tool_definitions = registry.get_definitions()
    except Exception:
        pass

    # Skills (always available via skills_loader)
    skill_list = []
    _sl = getattr(request.app.state, "skills_loader", None)
    if _sl:
        _skills = _sl.list_skills()
        skill_list = [{"name": s.name, "description": s.description} for s in _skills]

    # API key status - read directly from config file since Pydantic ignores tools.extra
    extra = {}
    try:
        import json as _json
        _cfg_path = Path.home() / ".grip" / "config.json"
        if _cfg_path.exists():
            _raw_cfg = _json.loads(_cfg_path.read_text())
            extra = _raw_cfg.get("tools", {}).get("extra", {})
    except Exception:
        pass

    api_keys = {}
    key_checks = {
        "elevenlabs": "elevenlabs_api_key",
        "groq": "groq_api_key",
        "apify": "apify_api_token",
        "serper": "serper_api_key",
        "tavily": "tavily_api_key",
    }
    for name, key_name in key_checks.items():
        val = extra.get(key_name, "")
        api_keys[name] = bool(val and str(val).strip())

    # Cache stats
    cache_stats = {"youtube": 0, "twitter": 0}
    cache_dir = ws_path / "cache"
    if cache_dir.exists():
        try:
            yt_cache = list(cache_dir.glob("youtube_*")) + list(cache_dir.glob("yt_*"))
            tw_cache = list(cache_dir.glob("twitter_*")) + list(cache_dir.glob("tw_*")) + list(cache_dir.glob("x_*"))
            cache_stats["youtube"] = len(yt_cache)
            cache_stats["twitter"] = len(tw_cache)
        except Exception:
            pass
    # Also check state directory for caches
    state_dir = ws_path / "state"
    if state_dir.exists():
        try:
            for f in state_dir.iterdir():
                fname = f.name.lower()
                if "youtube" in fname or "yt_cache" in fname:
                    try:
                        import json as _json
                        data = _json.loads(f.read_text())
                        if isinstance(data, dict):
                            cache_stats["youtube"] += len(data)
                    except Exception:
                        cache_stats["youtube"] += 1
                if "twitter" in fname or "x_cache" in fname:
                    try:
                        import json as _json
                        data = _json.loads(f.read_text())
                        if isinstance(data, dict):
                            cache_stats["twitter"] += len(data)
                    except Exception:
                        cache_stats["twitter"] += 1
        except Exception:
            pass

    # Session/message stats
    sessions_dir = ws_path / "sessions"
    session_count = 0
    today_messages = 0
    if sessions_dir.exists():
        import datetime
        today_str = datetime.date.today().isoformat()
        try:
            session_files = list(sessions_dir.glob("*.json"))
            session_count = len(session_files)
            for sf in session_files:
                try:
                    stat = sf.stat()
                    mod_date = datetime.date.fromtimestamp(stat.st_mtime).isoformat()
                    if mod_date == today_str:
                        import json as _json
                        data = _json.loads(sf.read_text())
                        msgs = data.get("messages", [])
                        today_messages += len(msgs)
                except Exception:
                    pass
        except Exception:
            pass

    # Last memory update
    last_memory_update = None
    mem_path = ws_path / "memory" / "MEMORY.md"
    if not mem_path.exists():
        mem_path = ws_path / "MEMORY.md"
    if mem_path.exists():
        try:
            import datetime
            last_memory_update = datetime.datetime.fromtimestamp(
                mem_path.stat().st_mtime
            ).isoformat()
        except Exception:
            pass

    # Semantic memory stats (best-effort only)
    semantic_memory = {
        "chroma_entries": 0,
        "daily_archives": 0,
        "monthly_archives": 0,
        "latest_daily": None,
    }
    try:
        semantic_memory["chroma_entries"] = int(SemanticMemory().count())
    except Exception:
        pass

    archives_root = _memory_archives_dir()
    daily_dir = archives_root / "daily"
    monthly_dir = archives_root / "monthly"
    semantic_memory["daily_archives"] = _safe_archive_count(daily_dir)
    semantic_memory["monthly_archives"] = _safe_archive_count(monthly_dir)
    semantic_memory["latest_daily"] = _latest_daily_archive(daily_dir)

    # Context flush telemetry (SDK engine; safe fallback for others)
    context_flush_info = {
        "context_tokens_estimated": 0,
        "context_flush_threshold": 15000,
        "context_flush_enabled": True,
        "dynamic_tool_selection_enabled": True,
    }
    try:
        engine = getattr(request.app.state, "engine", None)
        getter = getattr(engine, "get_context_flush_info", None)
        if callable(getter):
            info = getter()
            if isinstance(info, dict):
                context_flush_info.update(info)
    except Exception:
        pass

    # Channels status
    channels_status = {}
    from grip.config.schema import ChannelsConfig
    for name in ChannelsConfig.CHANNEL_NAMES:
        ch = getattr(config.channels, name, None)
        if ch and ch.is_active():
            channels_status[name] = "active"
        elif ch and ch.enabled:
            channels_status[name] = "no_token"
        else:
            channels_status[name] = "disabled"

    return {
        "model": model,
        "effort": effort,
        "version": __version__,
        "uptime_seconds": round(uptime_seconds, 1),
        "memory_size_bytes": memory_size,
        "history_entry_count": history_count,
        "cron_jobs": cron_jobs,
        "cron_count": len(cron_jobs),
        "tools": ([
            {"name": t.get("name", t.get("function", {}).get("name", "unknown")),
             "description": t.get("description", t.get("function", {}).get("description", ""))}
            for t in tool_definitions
        ] if tool_definitions else skill_list),
        "tool_count": len(tool_definitions) if tool_definitions else len(skill_list),
        "api_keys": api_keys,
        "cache_stats": cache_stats,
        "session_count": session_count,
        "today_messages": today_messages,
        "last_memory_update": last_memory_update,
        "semantic_memory": semantic_memory,
        "channels": channels_status,
        "context_tokens_estimated": context_flush_info.get("context_tokens_estimated", 0),
        "context_flush_threshold": context_flush_info.get("context_flush_threshold", 15000),
        "context_flush_enabled": context_flush_info.get("context_flush_enabled", True),
        "dynamic_tool_selection_enabled": context_flush_info.get("dynamic_tool_selection_enabled", True),
    }
