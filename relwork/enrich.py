"""Enrich accepted papers with venue/year and author affiliations for the graph.

- venue/year: exact-title match in the local top-tier corpus (optional, RELWORK_CORPUS_DB; paper search
  sqlite), else Semantic Scholar's venue string, else arXiv.
- affiliations: the first pages of the PDF, read by the local LLM. Scholarly APIs leave
  affiliations empty for most recent preprints, so the paper itself is the source.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
from pathlib import Path

import httpx
import pymupdf
from loguru import logger
from pydantic import BaseModel, Field

from relwork.graph import Graph
from relwork.llm import LLM
from relwork.scholar import Scholar, norm_title

SHORT_VENUES = [
    ("CVPR", r"computer vision and pattern recognition|\bcvpr"),
    ("ICCV", r"international conference on computer vision|\biccv"),
    ("ECCV", r"european conference on computer vision|\beccv"),
    ("NeurIPS", r"neural information processing|neurips"),
    ("ICLR", r"learning representations|\biclr"),
    ("ICML", r"international conference on machine learning|\bicml"),
    ("AAAI", r"\baaai"),
    ("ACL", r"association for computational linguistics|\bacl\b"),
    ("EMNLP", r"empirical methods in natural language|emnlp"),
    ("WACV", r"\bwacv|winter conference on applications"),
    ("ICRA", r"\bicra|robotics and automation"),
    ("CoRL", r"conference on robot learning|\bcorl"),
    ("TPAMI", r"pattern analysis and machine intelligence"),
]

AFFIL_SYSTEM = """Extract the author affiliations from the first page(s) of a paper.
Return institution names only (university, company, lab), in short canonical English form
(e.g. "UCLA", "Google DeepMind", "Tsinghua University"), deduplicated, in author order,
at most 8. Ignore emails, addresses and departments. If none are printed, return []."""


class Affiliations(BaseModel):
    affiliations: list[str] = Field(description="Institution names in author order")


def _short_venue(raw: str) -> str:
    low = raw.lower()
    for short, pat in SHORT_VENUES:
        if re.search(pat, low):
            return short + (" Findings" if "findings" in low else "")
    return ""


def _corpus_lookup(corpus: sqlite3.Connection | None, title: str) -> tuple[str, int, str] | None:
    if corpus is None:
        return None
    nt = norm_title(title)
    for venue, year, t, pdf in corpus.execute(
        "SELECT venue, year, title, pdf_url FROM papers WHERE lower(substr(title,1,24))=?",
        (title.lower()[:24],),
    ):
        if norm_title(t) == nt:
            return venue, int(year), pdf
    return None


async def _first_pages(client: httpx.AsyncClient, url: str, cache: Path) -> str:
    if not cache.exists():
        resp = await client.get(url, follow_redirects=True)
        resp.raise_for_status()
        cache.write_bytes(resp.content)
    with pymupdf.open(cache) as doc:
        return "".join(doc[i].get_text() for i in range(min(2, doc.page_count)))[:5000]


async def enrich(run_dir: Path) -> None:
    graph = Graph(run_dir / "graph.sqlite")
    graph.db.execute(
        "CREATE TABLE IF NOT EXISTS paper_meta (pid TEXT PRIMARY KEY, venue TEXT, year INTEGER, affiliations TEXT)"
    )
    corpus_env = os.environ.get("RELWORK_CORPUS_DB", "")
    corpus_path = Path(corpus_env) if corpus_env else None
    corpus = (
        sqlite3.connect(f"file:{corpus_path}?mode=ro", uri=True)
        if corpus_path and corpus_path.exists()
        else None
    )
    rows = graph.db.execute(
        "SELECT n.pid, n.title, n.venue, n.pub_date, n.arxiv_id FROM nodes n "
        "LEFT JOIN judgments j USING(pid) WHERE (n.is_seed=1 OR j.grade>=2) "
        "AND n.pid NOT IN (SELECT pid FROM paper_meta)"
    ).fetchall()
    logger.info("enriching {} papers", len(rows))
    pdf_dir = run_dir / "cache" / "pdf"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    llm = LLM()
    scholar = Scholar(run_dir / "cache" / "http.sqlite")
    sem = asyncio.Semaphore(4)

    async with httpx.AsyncClient(headers={"User-Agent": "relwork/0.1"}, timeout=120.0) as client:

        async def one(pid: str, title: str, s2_venue: str, pub: str, arxiv_id: str) -> None:
            hit = _corpus_lookup(corpus, title)
            year = int(pub[:4]) if pub[:4].isdigit() else None
            if hit:
                venue, year, pdf_url = hit[0].replace("NEURIPS", "NeurIPS"), hit[1], hit[2]
            else:
                venue = _short_venue(s2_venue or "") or "arXiv"
                pdf_url = ""
            if not arxiv_id and not pdf_url:
                found = await scholar.arxiv_by_title(title)
                arxiv_id = found.arxiv_id if found else ""
                if found and year is None:
                    year = int(found.pub_date[:4])
            if arxiv_id:
                pdf_url = f"https://arxiv.org/pdf/{arxiv_id}"
            affs: list[str] = []
            if pdf_url:
                try:
                    async with sem:
                        text = await _first_pages(client, pdf_url, pdf_dir / f"{pid}.pdf")
                        affs = (await llm.structured(AFFIL_SYSTEM, text, Affiliations, max_tokens=800)).affiliations
                except Exception as exc:  # noqa: BLE001 - a missing PDF only loses affiliations
                    logger.warning("affiliations failed for {}: {}", title[:50], exc)
            graph.db.execute("INSERT OR REPLACE INTO paper_meta VALUES (?,?,?,?)",
                             (pid, venue, year, json.dumps(affs, ensure_ascii=False)))
            graph.db.commit()
            logger.info("{} | {} {} | {}", title[:50], venue, year, ", ".join(affs[:4]))

        await asyncio.gather(*(one(*r) for r in rows))
    await llm.aclose()
    await scholar.aclose()
