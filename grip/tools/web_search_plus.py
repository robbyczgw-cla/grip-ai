"""Native web_search_plus backend for Grip SDK engine.

Provider routing:
- Serper: news/facts/current events
- Tavily: research/deep-dive queries
- Perplexity: complex questions requiring synthesized answers
- Exa: discovery/similar/alternatives

The module returns normalized search rows and a rendered text output suitable for
SDK tool responses.
"""

from __future__ import annotations

import os
import re
from typing import Any

import httpx

_TIMEOUT = httpx.Timeout(connect=10.0, read=25.0, write=10.0, pool=5.0)


def _get_key(extra: dict[str, Any] | None, extra_key: str, env_keys: list[str]) -> str:
    if isinstance(extra, dict):
        value = extra.get(extra_key)
        if value:
            return str(value)
    for env_key in env_keys:
        value = os.environ.get(env_key)
        if value:
            return value
    return ""


def choose_provider_order(query: str) -> list[str]:
    q = (query or "").lower()
    # Intent buckets tuned to requested order:
    # Serper (facts/news) -> Tavily (research) -> Perplexity (complex AI answers) -> Exa (discovery)
    fact_news = re.search(r"\b(news|latest|today|breaking|price|stock|score|weather|when|where|who is|what is)\b", q)
    research = re.search(r"\b(research|paper|study|analysis|compare|benchmark|deep\s*dive|methodology|pros and cons)\b", q)
    discovery = re.search(r"\b(similar|alternatives?|discover|find tools|startups|companies|examples?)\b", q)
    complex_q = len(q) > 120 or re.search(r"\b(explain|how does|why does|trade-?offs?|step by step|synthesize|summari[sz]e)\b", q)

    if fact_news:
        return ["serper", "tavily", "perplexity", "exa"]
    if research:
        return ["tavily", "perplexity", "serper", "exa"]
    if complex_q:
        return ["perplexity", "tavily", "serper", "exa"]
    if discovery or "http://" in q or "https://" in q:
        return ["exa", "serper", "tavily", "perplexity"]
    return ["serper", "tavily", "perplexity", "exa"]


async def _search_serper(client: httpx.AsyncClient, *, query: str, max_results: int, api_key: str) -> list[dict[str, str]]:
    if not api_key:
        return []
    resp = await client.post(
        "https://google.serper.dev/search",
        json={"q": query, "num": max_results},
        headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
    )
    resp.raise_for_status()
    data = resp.json()
    return [
        {
            "title": item.get("title", ""),
            "url": item.get("link", ""),
            "snippet": item.get("snippet", ""),
        }
        for item in data.get("organic", [])[:max_results]
    ]


async def _search_tavily(client: httpx.AsyncClient, *, query: str, max_results: int, api_key: str) -> list[dict[str, str]]:
    if not api_key:
        return []
    resp = await client.post(
        "https://api.tavily.com/search",
        json={
            "api_key": api_key,
            "query": query,
            "max_results": max_results,
            "topic": "general",
            "search_depth": "advanced" if len(query) > 100 else "basic",
        },
    )
    resp.raise_for_status()
    data = resp.json()
    return [
        {
            "title": item.get("title", ""),
            "url": item.get("url", ""),
            "snippet": item.get("content", "") or item.get("snippet", ""),
        }
        for item in data.get("results", [])[:max_results]
    ]


async def _search_perplexity(client: httpx.AsyncClient, *, query: str, max_results: int, api_key: str) -> list[dict[str, str]]:
    if not api_key:
        return []
    resp = await client.post(
        "https://api.kilo.ai/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": "sonar",
            "messages": [
                {"role": "system", "content": "Answer with concise web-grounded synthesis and cite sources."},
                {"role": "user", "content": query},
            ],
            "temperature": 0.2,
        },
    )
    resp.raise_for_status()
    data = resp.json()
    choice = (data.get("choices") or [{}])[0]
    text = ((choice.get("message") or {}).get("content") or "").strip()
    citations = data.get("citations") or []
    rows: list[dict[str, str]] = []
    if text:
        rows.append({"title": "Perplexity synthesized answer", "url": citations[0] if citations else "", "snippet": text[:1200]})
    for c in citations[: max(0, max_results - 1)]:
        rows.append({"title": "Citation", "url": c, "snippet": "Source"})
    return rows[:max_results]


async def _search_exa(client: httpx.AsyncClient, *, query: str, max_results: int, api_key: str) -> list[dict[str, str]]:
    if not api_key:
        return []
    resp = await client.post(
        "https://api.exa.ai/search",
        headers={"x-api-key": api_key, "Content-Type": "application/json"},
        json={"query": query, "numResults": max_results, "type": "auto"},
    )
    resp.raise_for_status()
    data = resp.json()
    return [
        {
            "title": item.get("title", ""),
            "url": item.get("url", ""),
            "snippet": (item.get("text") or "")[:400],
        }
        for item in data.get("results", [])[:max_results]
    ]


async def search_web_plus(query: str, max_results: int = 5, *, extra: dict[str, Any] | None = None) -> tuple[str, str, list[str]]:
    """Run routed web search and return (provider, rendered_text, errors)."""
    query = (query or "").strip()
    max_results = min(max(int(max_results or 5), 1), 10)

    keys = {
        "serper": _get_key(extra, "serper_api_key", ["SERPER_API_KEY"]),
        "tavily": _get_key(extra, "tavily_api_key", ["TAVILY_API_KEY"]),
        "perplexity": _get_key(extra, "perplexity_api_key", ["PERPLEXITY_API_KEY", "PPLX_API_KEY"]),
        "exa": _get_key(extra, "exa_api_key", ["EXA_API_KEY"]),
    }

    order = choose_provider_order(query)
    searchers = {
        "serper": _search_serper,
        "tavily": _search_tavily,
        "perplexity": _search_perplexity,
        "exa": _search_exa,
    }

    errors: list[str] = []
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        for provider in order:
            try:
                rows = await searchers[provider](client, query=query, max_results=max_results, api_key=keys[provider])
                if rows:
                    text = "\n\n".join(
                        f"[{provider}] **{r.get(title,)}**\n{r.get(url,)}\n{r.get(snippet,)}" for r in rows
                    )
                    return provider, text, errors
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{provider}:{type(exc).__name__}")

    return "", "", errors
