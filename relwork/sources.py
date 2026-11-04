"""Seed resolution and full-text fetching (arXiv first)."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pymupdf
import httpx
from loguru import logger
from pydantic import BaseModel

ARXIV_ID_RE = re.compile(r"(\d{4}\.\d{4,5})(v\d+)?")
_ATOM = "{http://www.w3.org/2005/Atom}"
_HEADERS = {"User-Agent": "relwork/0.1"}


class SeedMeta(BaseModel):
    arxiv_id: str
    title: str
    year: int
    published: str
    authors: list[str]
    abstract: str


def parse_arxiv_id(text: str) -> str | None:
    m = ARXIV_ID_RE.search(text)
    return m.group(1) if m else None


async def arxiv_meta(client: httpx.AsyncClient, arxiv_id: str) -> SeedMeta:
    resp = await client.get(
        "https://export.arxiv.org/api/query", params={"id_list": arxiv_id}
    )
    resp.raise_for_status()
    entry = ET.fromstring(resp.text).find(f"{_ATOM}entry")
    if entry is None:
        raise ValueError(f"arXiv id not found: {arxiv_id}")
    published = (entry.findtext(f"{_ATOM}published") or "").strip()
    return SeedMeta(
        arxiv_id=arxiv_id,
        title=" ".join((entry.findtext(f"{_ATOM}title") or "").split()),
        year=int(published[:4]),
        published=published[:10],
        authors=[
            (a.findtext(f"{_ATOM}name") or "").strip()
            for a in entry.findall(f"{_ATOM}author")
        ],
        abstract=" ".join((entry.findtext(f"{_ATOM}summary") or "").split()),
    )


async def arxiv_search_title(client: httpx.AsyncClient, title: str) -> str:
    resp = await client.get(
        "https://export.arxiv.org/api/query",
        params={"search_query": f'ti:"{title}"', "max_results": 1},
    )
    resp.raise_for_status()
    entry = ET.fromstring(resp.text).find(f"{_ATOM}entry")
    if entry is None:
        raise ValueError(f"no arXiv match for title: {title}")
    arxiv_id = parse_arxiv_id(entry.findtext(f"{_ATOM}id") or "")
    if arxiv_id is None:
        raise ValueError(f"unparseable arXiv id for title: {title}")
    return arxiv_id


async def resolve_seed(client: httpx.AsyncClient, spec: str) -> SeedMeta:
    arxiv_id = parse_arxiv_id(spec) or await arxiv_search_title(client, spec)
    return await arxiv_meta(client, arxiv_id)


async def fetch_pdf_text(client: httpx.AsyncClient, arxiv_id: str, cache_dir: Path) -> str:
    cache_dir.mkdir(parents=True, exist_ok=True)
    txt_path = cache_dir / f"{arxiv_id}.txt"
    if txt_path.exists():
        return txt_path.read_text()
    pdf_path = cache_dir / f"{arxiv_id}.pdf"
    if not pdf_path.exists():
        logger.info("downloading arXiv PDF {}", arxiv_id)
        resp = await client.get(f"https://arxiv.org/pdf/{arxiv_id}", follow_redirects=True)
        resp.raise_for_status()
        pdf_path.write_bytes(resp.content)
    with pymupdf.open(pdf_path) as doc:
        text = "".join(page.get_text() for page in doc)
    txt_path.write_text(text)
    return text


def split_references(text: str) -> tuple[str, str]:
    """Split full text into (body, references) at the last References heading.

    Appendices after the bibliography are kept in the body.
    """
    matches = list(re.finditer(r"^\s*(References|Bibliography|REFERENCES)\s*$", text, re.M))
    if not matches:
        return text, ""
    start = matches[-1].start()
    refs_and_after = text[start:]
    appendix = re.search(r"^\s*(A|Appendix)\s+[A-Z][^\n]{2,80}$", refs_and_after, re.M)
    if appendix and appendix.start() > 2000:
        return text[:start] + refs_and_after[appendix.start():], refs_and_after[: appendix.start()]
    return text[:start], refs_and_after


def http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(headers=_HEADERS, timeout=httpx.Timeout(30.0, read=120.0))
