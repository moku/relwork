"""OpenAlex citation-graph client with the same interface as graph.S2Client.

Used when RELWORK_OPENALEX_API_KEY is set (Semantic Scholar refused us a key, and its
key-less pool is often throttled for many minutes). Nodes are returned in Semantic
Scholar's shape so the graph store and the LLM steps stay source-agnostic.

Differences from S2: no citing sentences / intents / influential flag on edges, and
arXiv preprints and their published versions can be separate works.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from typing import Any

import httpx
from loguru import logger

from relwork.scholar import HttpCache, norm_title

OA = "https://api.openalex.org"
SELECT = "id,title,abstract_inverted_index,publication_date,publication_year,primary_location,locations,ids,cited_by_count"
MAX_CITATIONS = 1000
_ARXIV_RE = re.compile(r"arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5})|10\.48550/arxiv\.(\d{4}\.\d{4,5})", re.I)


def _abstract(inv: dict[str, list[int]] | None) -> str:
    if not inv:
        return ""
    return " ".join(w for _, w in sorted((p, w) for w, ps in inv.items() for p in ps))


def _arxiv_id(work: dict[str, Any]) -> str:
    urls = [(work.get("ids") or {}).get("doi") or ""]
    for loc in work.get("locations") or []:
        urls += [loc.get("landing_page_url") or "", loc.get("pdf_url") or ""]
    for u in urls:
        m = _ARXIV_RE.search(u)
        if m:
            return m.group(1) or m.group(2)
    return ""


def _short_id(url: str) -> str:
    return url.rsplit("/", 1)[-1]


def to_node(work: dict[str, Any]) -> dict[str, Any]:
    """OpenAlex work -> Semantic Scholar-shaped node dict."""
    source = ((work.get("primary_location") or {}).get("source") or {}).get("display_name") or ""
    arxiv = _arxiv_id(work)
    return {
        "paperId": _short_id(work["id"]),
        "title": work.get("title") or "",
        "abstract": _abstract(work.get("abstract_inverted_index")),
        "publicationDate": work.get("publication_date") or "",
        "year": work.get("publication_year"),
        "venue": source,
        "externalIds": {"ArXiv": arxiv} if arxiv else {},
        "citationCount": work.get("cited_by_count") or 0,
    }


class OpenAlexClient:
    def __init__(self, cache_path: Path, api_key: str) -> None:
        self._cache = HttpCache(cache_path)
        self._client = httpx.AsyncClient(
            headers={"User-Agent": "relwork/0.1", "Authorization": f"Bearer {api_key}"},
            timeout=httpx.Timeout(30.0, read=120.0),
        )
        self._lock = asyncio.Lock()
        self._last = 0.0
        self._interval = 0.15

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any] | None:
        url = str(httpx.URL(f"{OA}{path}", params=params))
        cached = self._cache.get(url)
        if cached is not None:
            return json.loads(cached) if cached else None
        for attempt in range(8):
            async with self._lock:
                wait = self._interval - (time.monotonic() - self._last)
                if wait > 0:
                    await asyncio.sleep(wait)
                try:
                    resp = await self._client.get(url)
                except httpx.HTTPError as exc:
                    logger.warning("openalex transport error {}: {}", path, exc)
                    resp = None
                self._last = time.monotonic()
            if resp is not None and resp.status_code == 200:
                self._cache.put(url, resp.text)
                return resp.json()
            if resp is not None and resp.status_code == 404:
                self._cache.put(url, "")
                return None
            if resp is not None and resp.status_code in (401, 403):
                raise RuntimeError(f"openalex refused the key ({resp.status_code}): {resp.text[:200]}")
            if resp is not None and resp.status_code == 429 and "budget" in resp.text.lower():
                raise RuntimeError(f"openalex daily budget exhausted: {resp.text[:200]}")
            logger.warning("openalex {} on {} (attempt {})",
                           resp.status_code if resp is not None else "transport", path, attempt + 1)
            await asyncio.sleep(min(30.0, 2.0 * (attempt + 1)))
        logger.error("openalex gave up on {}", path)
        return None

    async def by_arxiv(self, arxiv_id: str) -> dict[str, Any] | None:
        """The best-cited version (arXiv preprint and proceedings are separate works)."""
        work = await self._get(f"/works/doi:10.48550/arXiv.{arxiv_id}", {"select": SELECT})
        if not work:
            return None
        versions = await self._versions(work.get("title") or "")
        best = max(versions + [work], key=lambda w: w.get("cited_by_count") or 0)
        node = to_node(best)
        node["externalIds"] = {"ArXiv": arxiv_id}
        return node

    async def _versions(self, title: str) -> list[dict[str, Any]]:
        """All works with exactly this title (preprint, proceedings, journal copies)."""
        clean = re.sub(r"[^A-Za-z0-9 ]+", " ", title)
        data = await self._get("/works", {"filter": f"title.search:{clean}", "select": SELECT, "per-page": 10})
        return [w for w in (data or {}).get("results") or []
                if norm_title(w.get("title") or "") == norm_title(title)]

    async def paper(self, pid: str) -> dict[str, Any] | None:
        work = await self._get(f"/works/{pid}", {"select": SELECT})
        return to_node(work) if work else None

    async def match_title(self, title: str) -> dict[str, Any] | None:
        clean = re.sub(r"[^A-Za-z0-9 ]+", " ", title)
        data = await self._get("/works", {"filter": f"title.search:{clean}", "select": SELECT, "per-page": 5})
        for work in (data or {}).get("results") or []:
            if norm_title(work.get("title") or "") == norm_title(title):
                return to_node(work)
        return None

    async def _works(self, flt: str, limit: int, sort: str = "") -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        cursor = "*"
        while cursor and len(out) < limit:
            params: dict[str, Any] = {"filter": flt, "select": SELECT, "per-page": 200, "cursor": cursor}
            if sort:
                params["sort"] = sort
            data = await self._get("/works", params)
            if not data:
                break
            out += data.get("results") or []
            cursor = (data.get("meta") or {}).get("next_cursor")
        return out[:limit]

    async def edges(self, pid: str, direction: str) -> list[dict[str, Any]]:
        """direction='references' (backward) or 'citations' (forward, newest first)."""
        # Union over all versions of the paper: citers and reference lists are split
        # between the arXiv preprint and the proceedings copy.
        work = await self._get(f"/works/{pid}", {"select": "id,title,referenced_works"})
        if not work:
            return []
        versions = {pid} | {_short_id(w["id"]) for w in await self._versions(work.get("title") or "")}
        works: list[dict[str, Any]] = []
        if direction == "references":
            ids: list[str] = []
            for v in sorted(versions):
                vw = work if v == pid else await self._get(f"/works/{v}", {"select": "id,referenced_works"})
                ids += [_short_id(u) for u in (vw or {}).get("referenced_works") or [] if _short_id(u) not in ids]
            for i in range(0, len(ids), 100):
                works += await self._works("openalex:" + "|".join(ids[i : i + 100]), 100)
        else:
            works = await self._works("cites:" + "|".join(sorted(versions)), MAX_CITATIONS,
                                      sort="publication_date:desc")
        seen: set[str] = set()
        works = [w for w in works if not (norm_title(w.get("title") or "") in seen
                                          or seen.add(norm_title(w.get("title") or "")))]
        return [
            {"node": to_node(w), "contexts": [], "intents": [], "influential": False}
            for w in works if w.get("title")
        ]
