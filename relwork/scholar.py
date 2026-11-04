"""Scholarly sources for candidate discovery, with an on-disk HTTP cache.

Recent (last-months) papers are poorly covered by citation graphs: OpenAlex often has
no reference list for arXiv preprints and almost no citers yet, and Semantic Scholar
throttles key-less callers hard. So discovery leans on arXiv search (date-ranged,
newest first), OpenAlex search, and bibliographies parsed from arXiv full text.
"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import time
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path
from typing import Any

import httpx
from loguru import logger
from pydantic import BaseModel

from relwork.sources import parse_arxiv_id

_ATOM = "{http://www.w3.org/2005/Atom}"
_ARXIV_API = "https://export.arxiv.org/api/query"
_OPENALEX = "https://api.openalex.org"
_ARXIV_MIN_INTERVAL = 3.1
# Typical conference month, used to place venue-year papers on the timeline.
_VENUE_MONTH = {
    "AAAI": 2, "WACV": 3, "ICLR": 4, "ICRA": 5, "CVPR": 6, "ICML": 7, "RSS": 7,
    "ECCV": 9, "ICCV": 10, "IROS": 10, "NEURIPS": 12,
}
_STOPWORDS = {
    "a", "an", "the", "of", "for", "and", "or", "in", "on", "with", "to", "via", "by",
    "using", "based", "from", "as", "at", "into", "model", "models",
}


class Paper(BaseModel):
    key: str
    title: str
    abstract: str = ""
    pub_date: str = ""
    arxiv_id: str = ""
    venue: str = ""
    source: str = ""


def norm_title(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


def paper_key(arxiv_id: str, title: str) -> str:
    return f"arxiv:{arxiv_id}" if arxiv_id else f"title:{norm_title(title)[:120]}"


class HttpCache:
    def __init__(self, path: Path) -> None:
        self._db = sqlite3.connect(path)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS http (url TEXT PRIMARY KEY, body TEXT, ts REAL)"
        )

    def get(self, url: str) -> str | None:
        row = self._db.execute("SELECT body FROM http WHERE url=?", (url,)).fetchone()
        return row[0] if row else None

    def put(self, url: str, body: str) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO http VALUES (?,?,?)", (url, body, time.time())
        )
        self._db.commit()


class Scholar:
    def __init__(
        self, cache_path: Path, openalex_key: str = "", paper_search_url: str = ""
    ) -> None:
        self._paper_search_url = paper_search_url.rstrip("/")
        self._cache = HttpCache(cache_path)
        self._client = httpx.AsyncClient(
            headers={"User-Agent": "relwork/0.1"},
            timeout=httpx.Timeout(30.0, read=120.0),
            follow_redirects=True,
        )
        self._arxiv_lock = asyncio.Lock()
        self._arxiv_last = 0.0
        self._openalex_key = openalex_key

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(self, url: str, params: dict[str, Any], *, arxiv: bool = False) -> str:
        full = str(httpx.URL(url, params=params))
        cached = self._cache.get(full)
        if cached is not None:
            return cached
        for attempt in range(4):
            if arxiv:
                async with self._arxiv_lock:
                    wait = _ARXIV_MIN_INTERVAL - (time.monotonic() - self._arxiv_last)
                    if wait > 0:
                        await asyncio.sleep(wait)
                    resp = await self._client.get(full)
                    self._arxiv_last = time.monotonic()
            else:
                resp = await self._client.get(full)
            if resp.status_code in (429, 500, 502, 503):
                logger.warning("{} {} (attempt {})", resp.status_code, url, attempt + 1)
                await asyncio.sleep(5 * (attempt + 1))
                continue
            resp.raise_for_status()
            self._cache.put(full, resp.text)
            return resp.text
        raise RuntimeError(f"giving up on {url}")

    # ---- arXiv -------------------------------------------------------------

    @staticmethod
    def _arxiv_entries(xml_text: str, source: str) -> list[Paper]:
        papers = []
        for entry in ET.fromstring(xml_text).findall(f"{_ATOM}entry"):
            arxiv_id = parse_arxiv_id(entry.findtext(f"{_ATOM}id") or "")
            title = " ".join((entry.findtext(f"{_ATOM}title") or "").split())
            if not arxiv_id or not title:
                continue
            papers.append(
                Paper(
                    key=paper_key(arxiv_id, title),
                    title=title,
                    abstract=" ".join((entry.findtext(f"{_ATOM}summary") or "").split()),
                    pub_date=(entry.findtext(f"{_ATOM}published") or "")[:10],
                    arxiv_id=arxiv_id,
                    venue="arXiv",
                    source=source,
                )
            )
        return papers

    async def arxiv_search(
        self, query: str, start: date, end: date, max_results: int = 50
    ) -> list[Paper]:
        terms = [w for w in re.findall(r"[A-Za-z0-9\-]+", query) if w.lower() not in _STOPWORDS]
        date_clause = f"submittedDate:[{start:%Y%m%d}0000 TO {end:%Y%m%d}2359]"
        results: list[Paper] = []
        # Full AND first; if too narrow, relax to the first 3 terms.
        for subset in (terms, terms[:3]):
            if not subset:
                break
            q = " AND ".join(f"all:{t}" for t in subset) + f" AND {date_clause}"
            body = await self._get(
                _ARXIV_API,
                {
                    "search_query": q,
                    "sortBy": "submittedDate",
                    "sortOrder": "descending",
                    "max_results": max_results,
                },
                arxiv=True,
            )
            results = self._arxiv_entries(body, f"arxiv-search:{query}")
            if len(results) >= 5 or len(subset) <= 3:
                break
        return results

    async def arxiv_by_ids(self, ids: list[str]) -> list[Paper]:
        papers: list[Paper] = []
        for i in range(0, len(ids), 50):
            chunk = ids[i : i + 50]
            body = await self._get(
                _ARXIV_API, {"id_list": ",".join(chunk), "max_results": len(chunk)}, arxiv=True
            )
            papers.extend(self._arxiv_entries(body, "arxiv-id"))
        return papers

    async def arxiv_by_title(self, title: str) -> Paper | None:
        words = [w for w in re.findall(r"[A-Za-z0-9]+", title) if w.lower() not in _STOPWORDS]
        if not words:
            return None
        q = " AND ".join(f"ti:{w}" for w in words[:8])
        body = await self._get(_ARXIV_API, {"search_query": q, "max_results": 3}, arxiv=True)
        target = norm_title(title)
        for paper in self._arxiv_entries(body, "arxiv-title"):
            cand = norm_title(paper.title)
            if cand == target or cand.startswith(target) or target.startswith(cand):
                return paper
        return None

    # ---- OpenAlex ----------------------------------------------------------

    @staticmethod
    def _openalex_abstract(inv: dict[str, list[int]] | None) -> str:
        if not inv:
            return ""
        pos = sorted((p, w) for w, ps in inv.items() for p in ps)
        return " ".join(w for _, w in pos)

    def _openalex_paper(self, w: dict[str, Any], source: str) -> Paper:
        doi = (w.get("doi") or "").lower()
        arxiv_id = ""
        m = re.search(r"arxiv\.(\d{4}\.\d{4,5})", doi)
        if m:
            arxiv_id = m.group(1)
        title = w.get("title") or ""
        venue = ((w.get("primary_location") or {}).get("source") or {}).get("display_name") or ""
        return Paper(
            key=paper_key(arxiv_id, title),
            title=title,
            abstract=self._openalex_abstract(w.get("abstract_inverted_index")),
            pub_date=w.get("publication_date") or "",
            arxiv_id=arxiv_id,
            venue=venue,
            source=source,
        )

    async def openalex_search(
        self, query: str, start: date, end: date, per_page: int = 50
    ) -> list[Paper]:
        params: dict[str, Any] = {
            "search": query,
            "filter": f"from_publication_date:{start},to_publication_date:{end}",
            "per-page": per_page,
            "select": "id,doi,title,publication_date,abstract_inverted_index,primary_location",
        }
        if self._openalex_key:
            params["api_key"] = self._openalex_key
        try:
            body = await self._get(f"{_OPENALEX}/works", params)
        except (RuntimeError, httpx.HTTPError) as exc:
            logger.warning("openalex search failed: {}", exc)
            return []
        data = json.loads(body)
        return [
            self._openalex_paper(w, f"openalex-search:{query}")
            for w in data.get("results", [])
            if w.get("title")
        ]

    # ---- optional local paper-search service (RELWORK_PAPER_SEARCH_URL) ---

    async def paper_search(
        self, query: str, start: date, end: date, top_k: int = 50
    ) -> list[Paper]:
        if not self._paper_search_url:
            return []
        papers: list[Paper] = []
        for year in range(max(start.year, 2015), end.year + 1):
            url = f"{self._paper_search_url}/search"
            cache_key = f"{url}?q={query}&year={year}&k={top_k}"
            body = self._cache.get(cache_key)
            if body is None:
                try:
                    resp = await self._client.post(
                        url, json={"query": query, "top_k": top_k, "year_filter": str(year)}
                    )
                    resp.raise_for_status()
                except httpx.HTTPError as exc:
                    logger.warning("paper search failed: {}", exc)
                    return papers
                body = resp.text
                self._cache.put(cache_key, body)
            for r in json.loads(body).get("results", []):
                venue = str(r.get("venue", ""))
                pub = date(int(r["year"]), _VENUE_MONTH.get(venue.upper(), 6), 15)
                if not start <= pub <= end:
                    continue
                title = r.get("title") or ""
                papers.append(
                    Paper(
                        key=paper_key("", title),
                        title=title,
                        abstract=r.get("abstract") or "",
                        pub_date=pub.isoformat(),
                        venue=f"{venue} {r['year']}",
                        source=f"paper-search:{query}",
                    )
                )
        return papers
