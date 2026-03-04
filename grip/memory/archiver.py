from __future__ import annotations

from datetime import UTC, date as date_cls, datetime
from pathlib import Path

import requests

GROQ_CHAT_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_SUMMARY_MODEL = "llama-3.3-70b-versatile"


def _memory_root() -> Path:
    root = Path.home() / ".grip" / "memory"
    root.mkdir(parents=True, exist_ok=True)
    (root / "daily").mkdir(parents=True, exist_ok=True)
    (root / "monthly").mkdir(parents=True, exist_ok=True)
    return root


def _discover_history_files() -> list[Path]:
    roots = [Path.home() / ".grip" / "workspaces", Path.home() / ".openclaw"]
    out: list[Path] = []
    for base in roots:
        if not base.exists():
            continue
        out.extend(base.glob("**/memory/HISTORY.md"))
    # newest first
    return sorted((p for p in out if p.exists()), key=lambda p: p.stat().st_mtime, reverse=True)


def _read_history_for_date(iso_date: str) -> str:
    lines: list[str] = []
    for history_path in _discover_history_files():
        try:
            for line in history_path.read_text(encoding="utf-8", errors="ignore").splitlines():
                if line.startswith(f"[{iso_date} "):
                    lines.append(line)
        except Exception:
            continue
    return "\n".join(lines).strip()


def _groq_summarize(prompt: str, groq_api_key: str) -> str:
    if not groq_api_key:
        raise ValueError("groq_api_key is required")
    resp = requests.post(
        GROQ_CHAT_URL,
        headers={
            "Authorization": f"Bearer {groq_api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": GROQ_SUMMARY_MODEL,
            "temperature": 0.2,
            "messages": [
                {
                    "role": "system",
                    "content": "You are a concise memory archivist. Extract key topics, decisions, facts, follow-ups, and notable context.",
                },
                {"role": "user", "content": prompt},
            ],
        },
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    return (
        (((data.get("choices") or [{}])[0].get("message") or {}).get("content"))
        or "No summary generated."
    ).strip()


def create_daily_summary(date: str, groq_api_key: str) -> str:
    """Generate and save a daily memory summary to ~/.grip/memory/daily/YYYY-MM-DD.md."""
    datetime.strptime(date, "%Y-%m-%d")  # validate
    history_text = _read_history_for_date(date)
    if not history_text:
        summary = f"# Daily Memory Archive: {date}\n\nNo history entries found for this date."
    else:
        prompt = (
            f"Create a daily memory archive for {date}.\n"
            "Include:\n"
            "- Main topics\n- Key decisions\n- Important facts learned\n"
            "- Open questions / follow-ups\n- Notable user preferences\n\n"
            f"History entries:\n{history_text}"
        )
        body = _groq_summarize(prompt, groq_api_key)
        summary = f"# Daily Memory Archive: {date}\n\n{body}\n"

    out_file = _memory_root() / "daily" / f"{date}.md"
    out_file.write_text(summary, encoding="utf-8")
    return str(out_file)


def create_monthly_summary(year: int, month: int, groq_api_key: str | None = None) -> str:
    """Generate and save a monthly digest to ~/.grip/memory/monthly/YYYY-MM.md."""
    if month < 1 or month > 12:
        raise ValueError("month must be in 1..12")

    ym = f"{year:04d}-{month:02d}"
    daily_dir = _memory_root() / "daily"
    daily_files = sorted(daily_dir.glob(f"{ym}-*.md"))

    if not daily_files:
        monthly_text = f"# Monthly Memory Digest: {ym}\n\nNo daily summaries found for this month."
    else:
        combined = []
        for f in daily_files:
            combined.append(f"## {f.stem}\n")
            combined.append(f.read_text(encoding="utf-8", errors="ignore"))
            combined.append("\n")
        joined = "\n".join(combined)

        if groq_api_key:
            prompt = (
                f"Create a monthly memory digest for {ym}.\n"
                "Summarize recurring themes, major decisions, important facts, unresolved threads, and suggested priorities.\n\n"
                f"Daily archives:\n{joined}"
            )
            body = _groq_summarize(prompt, groq_api_key)
        else:
            body = joined[:12000]
        monthly_text = f"# Monthly Memory Digest: {ym}\n\n{body}\n"

    out_file = _memory_root() / "monthly" / f"{ym}.md"
    out_file.write_text(monthly_text, encoding="utf-8")
    return str(out_file)


def list_archives() -> dict[str, list[str]]:
    root = _memory_root()
    return {
        "daily": sorted([p.name for p in (root / "daily").glob("*.md")]),
        "monthly": sorted([p.name for p in (root / "monthly").glob("*.md")]),
    }


def read_archive(identifier: str) -> str:
    root = _memory_root()
    candidate = identifier.strip()
    if not candidate:
        raise ValueError("identifier is required")

    if len(candidate) == 10:
        path = root / "daily" / f"{candidate}.md"
    elif len(candidate) == 7:
        path = root / "monthly" / f"{candidate}.md"
    else:
        # allow direct filename fallback
        day_path = root / "daily" / candidate
        month_path = root / "monthly" / candidate
        path = day_path if day_path.exists() else month_path

    if not path.exists():
        raise FileNotFoundError(f"Archive not found: {identifier}")
    return path.read_text(encoding="utf-8", errors="ignore")


def today_iso() -> str:
    return datetime.now(UTC).date().isoformat()


def previous_month(today: date_cls | None = None) -> tuple[int, int]:
    d = today or datetime.now(UTC).date()
    y, m = d.year, d.month
    if m == 1:
        return (y - 1, 12)
    return (y, m - 1)
