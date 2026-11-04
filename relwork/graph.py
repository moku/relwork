"""Stage 3: citation-graph exploration (reference map only, no semantic search).

Candidates come exclusively from citation edges around the seeds:
- backward: what a node cites (its references)
- forward:  who cites a node (its citations)
starting from the seeds and expanding only through nodes the LLM accepted, up to
``max_depth`` hops. Every edge carries the citing sentences (Semantic Scholar
contexts), so the LLM decides whether to follow it from *why* it is cited.

Ordering is recency-first: discovered nodes are screened, judged and expanded
newest first, and reports are rewritten after every round, so an interrupted run
still has its most recent work mapped.
"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import httpx
import yaml
from loguru import logger

from relwork.llm import LLM
from relwork.models import FacetSet, JudgeResult, TriageResult
from relwork.openalex import OpenAlexClient
from relwork.scholar import HttpCache, norm_title

S2 = "https://api.semanticscholar.org/graph/v1"
NODE_FIELDS = "paperId,title,abstract,publicationDate,year,venue,externalIds,citationCount"
EDGE_FIELDS = "contexts,intents,isInfluential," + NODE_FIELDS
TRIAGE_BATCH = 25
JUDGE_BATCH = 6
LLM_CONCURRENCY = 4
EXPAND_PER_ROUND = 10
MAX_ROUNDS = 6
MIN_NEW_ACCEPTED = 2
MAX_CITATIONS = 1000
PRIORITY_RANK = {"high": 0, "medium": 1, "low": 2}


def _neg_date(d: str) -> str:
    """Sort key that orders ISO dates newest first."""
    return "".join(chr(0x7E - ord(ch)) if ch.isdigit() else ch for ch in (d or "0000"))


# ---- Semantic Scholar client ------------------------------------------------


# About 4 minutes of backoff. S2 is worth a short wait: it has the better coverage and
# citing sentences, but a key-less 429 window can last far longer than this.
S2_PROBE_ATTEMPTS = 15


class S2Client:
    max_attempts = 40

    def __init__(self, cache_path: Path, api_key: str = "") -> None:
        self.gave_up = False
        self._cache = HttpCache(cache_path)
        headers = {"User-Agent": "relwork/0.1"}
        if api_key:
            headers["x-api-key"] = api_key
        self._interval = 1.05 if api_key else 1.5
        self._client = httpx.AsyncClient(headers=headers, timeout=httpx.Timeout(30.0, read=120.0))
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any] | None:
        url = str(httpx.URL(f"{S2}{path}", params=params))
        cached = self._cache.get(url)
        if cached is not None:
            return json.loads(cached) if cached else None
        # Key-less callers share one global S2 pool; 429 windows can last many minutes.
        for attempt in range(self.max_attempts):
            async with self._lock:
                wait = self._interval - (time.monotonic() - self._last)
                if wait > 0:
                    await asyncio.sleep(wait)
                try:
                    resp = await self._client.get(url)
                except httpx.HTTPError as exc:
                    logger.warning("s2 transport error {}: {}", path, exc)
                    resp = None
                self._last = time.monotonic()
            if resp is not None and resp.status_code == 200:
                self._cache.put(url, resp.text)
                return resp.json()
            if resp is not None and resp.status_code == 404:
                self._cache.put(url, "")
                return None
            status = resp.status_code if resp is not None else "transport"
            if attempt % 5 == 4:
                logger.warning("s2 {} on {} (attempt {})", status, path, attempt + 1)
            await asyncio.sleep(min(60.0, 2.0 * (attempt + 1)))
        logger.error("s2 gave up on {}", path)
        self.gave_up = True
        return None

    async def match_title(self, title: str) -> dict[str, Any] | None:
        data = await self._get("/paper/search/match", {"query": title, "fields": NODE_FIELDS})
        hits = (data or {}).get("data") or []
        return hits[0] if hits else None

    async def paper(self, pid: str) -> dict[str, Any] | None:
        return await self._get(f"/paper/{pid}", {"fields": NODE_FIELDS})

    async def by_arxiv(self, arxiv_id: str) -> dict[str, Any] | None:
        return await self.paper(f"arXiv:{arxiv_id}")

    async def edges(self, pid: str, direction: str) -> list[dict[str, Any]]:
        """direction='references' (backward) or 'citations' (forward)."""
        inner = "citedPaper" if direction == "references" else "citingPaper"
        out: list[dict[str, Any]] = []
        offset = 0
        while offset < MAX_CITATIONS:
            data = await self._get(
                f"/paper/{pid}/{direction}",
                {"fields": EDGE_FIELDS, "limit": 500, "offset": offset},
            )
            if not data:
                break
            for e in data.get("data") or []:
                node = e.get(inner) or {}
                if node.get("paperId") and node.get("title"):
                    out.append({"node": node, "contexts": e.get("contexts") or [],
                                "intents": e.get("intents") or [],
                                "influential": bool(e.get("isInfluential"))})
            if "next" not in data:
                break
            offset = data["next"]
        return out


# ---- graph store -------------------------------------------------------------


class Graph:
    def __init__(self, path: Path) -> None:
        self.db = sqlite3.connect(path)
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS nodes (
                pid TEXT PRIMARY KEY, title TEXT, norm_title TEXT, abstract TEXT,
                pub_date TEXT, venue TEXT, arxiv_id TEXT, citation_count INTEGER,
                depth INTEGER, is_seed INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS edges (
                src TEXT, dst TEXT, contexts TEXT, intents TEXT, influential INTEGER,
                PRIMARY KEY (src, dst));
            CREATE TABLE IF NOT EXISTS triage (pid TEXT PRIMARY KEY, facet TEXT, round INTEGER);
            CREATE TABLE IF NOT EXISTS judgments (
                pid TEXT PRIMARY KEY, round INTEGER, facet TEXT, secondary TEXT,
                relation TEXT, grade INTEGER, reason TEXT, contrast TEXT);
            CREATE TABLE IF NOT EXISTS expanded (pid TEXT, direction TEXT, n INTEGER,
                PRIMARY KEY (pid, direction));
            CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
            """
        )

    def upsert_node(self, node: dict[str, Any], depth: int, is_seed: bool = False) -> bool:
        pid = node["paperId"]
        row = self.db.execute("SELECT depth FROM nodes WHERE pid=?", (pid,)).fetchone()
        if row:
            if depth < row[0]:
                self.db.execute("UPDATE nodes SET depth=? WHERE pid=?", (depth, pid))
            return False
        ext = node.get("externalIds") or {}
        pub = node.get("publicationDate") or (f"{node['year']}-06-15" if node.get("year") else "")
        self.db.execute(
            "INSERT INTO nodes VALUES (?,?,?,?,?,?,?,?,?,?)",
            (pid, node["title"], norm_title(node["title"]), node.get("abstract") or "", pub,
             node.get("venue") or "", ext.get("ArXiv") or "", node.get("citationCount") or 0,
             depth, int(is_seed)),
        )
        return True

    def add_edge(self, src: str, dst: str, e: dict[str, Any]) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO edges VALUES (?,?,?,?,?)",
            (src, dst, json.dumps(e["contexts"][:3]), json.dumps(e["intents"]), int(e["influential"])),
        )

    def contexts_for(self, pid: str) -> list[str]:
        """Citing sentences on edges touching this node, labelled by the other paper."""
        rows = self.db.execute(
            "SELECT n.title, e.contexts, e.src=? FROM edges e JOIN nodes n "
            "ON n.pid = CASE WHEN e.src=? THEN e.dst ELSE e.src END "
            "WHERE e.src=? OR e.dst=? LIMIT 6",
            (pid, pid, pid, pid),
        ).fetchall()
        out = []
        for other, ctx, is_src in rows:
            for c in json.loads(ctx)[:2]:
                who = f"this paper citing '{other[:60]}'" if is_src else f"'{other[:60]}' citing this paper"
                out.append(f"{who}: {c[:300]}")
        return out[:6]

    def nodes_where(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        cur = self.db.execute(
            "SELECT pid,title,abstract,pub_date,venue,arxiv_id,citation_count,depth FROM nodes " + sql,
            params,
        )
        keys = ["pid", "title", "abstract", "pub_date", "venue", "arxiv_id", "citation_count", "depth"]
        return [dict(zip(keys, r)) for r in cur.fetchall()]


# ---- LLM steps ---------------------------------------------------------------

TRIAGE_SYSTEM = """You screen papers found in the citation neighbourhood of a seed paper, for a
researcher's related-work map. For each numbered paper (title, abstract excerpt, and the
sentences in which it is cited / it cites) pick the single best-matching facet id or 'none'.
Be strict: generic backbones (LLMs, VLMs, video generators used only as tools), datasets cited
in passing, and papers sharing only buzzwords are 'none' unless a facet explicitly covers them.
When unsure between a facet and 'none', choose the facet (a later step reads more)."""

JUDGE_SYSTEM = """You judge papers from the citation neighbourhood of a seed paper against a
researcher's facets and research context. Use the abstract AND the citing sentences.
For each numbered paper return primary facet (or 'none'), secondary facets, relation type,
grade and a short Korean reason.

Grade rubric (apply strictly, most candidates are 1 or 0):
- 3 direct competitor: implements a core mechanism of the researcher's idea or the seed's core
  contribution (would have to be cited and compared against).
- 2 related: clearly satisfies a facet's include criteria.
- 1 peripheral: same broad topic, but fails include criteria or hits an exclude item.
- 0 unrelated.
contrast: for grade>=2, one Korean sentence on how it differs from the researcher's idea."""


def _facet_brief(facets: FacetSet) -> str:
    return "\n".join(
        f"[{f.id}] ({f.priority}) {f.name}: {f.description}\n"
        f"  include: {'; '.join(f.include)}\n  exclude: {'; '.join(f.exclude)}"
        for f in facets.facets
    )


def _block(i: int, n: dict[str, Any], contexts: list[str], abstract_chars: int) -> str:
    abstract = (n["abstract"] or "(no abstract)")[:abstract_chars]
    ctx = "\n".join(f"  cite: {c}" for c in contexts[: (2 if abstract_chars < 1000 else 4)])
    return f"[{i}] {n['title']} ({n['pub_date']}, {n['venue']})\n{abstract}\n{ctx}"


async def _batched(llm: LLM, system: str, head: str, items: list[dict[str, Any]], graph: Graph,
                   schema: type, batch: int, chars: int, max_tokens: int) -> list[tuple[dict[str, Any], Any]]:
    sem = asyncio.Semaphore(LLM_CONCURRENCY)

    async def one(chunk: list[dict[str, Any]]) -> list[tuple[dict[str, Any], Any]]:
        user = head + "\n\n".join(
            _block(i, n, graph.contexts_for(n["pid"]), chars) for i, n in enumerate(chunk)
        )
        async with sem:
            try:
                res = await llm.structured(system, user, schema, max_tokens=max_tokens)
            except RuntimeError as exc:
                logger.warning("llm batch failed: {}", exc)
                return []
        return [(chunk[it.idx], it) for it in res.items if 0 <= it.idx < len(chunk)]

    chunks = [items[i : i + batch] for i in range(0, len(items), batch)]
    out: list[tuple[dict[str, Any], Any]] = []
    for part in await asyncio.gather(*(one(c) for c in chunks)):
        out += part
    return out


# ---- crawl -------------------------------------------------------------------


async def _expand(s2: S2Client | OpenAlexClient, graph: Graph, node: dict[str, Any], direction: str, max_depth: int) -> int:
    edges = await s2.edges(node["pid"], direction)
    new = 0
    for e in edges:
        nb = e["node"]
        new += graph.upsert_node(nb, node["depth"] + 1)
        if direction == "references":
            graph.add_edge(node["pid"], nb["paperId"], e)
        else:
            graph.add_edge(nb["paperId"], node["pid"], e)
    graph.db.execute("INSERT OR REPLACE INTO expanded VALUES (?,?,?)", (node["pid"], direction, len(edges)))
    graph.db.commit()
    logger.info("expand {} {} [{}] -> {} edges, {} new", direction, node["title"][:50], node["depth"], len(edges), new)
    return new


async def _backfill_seed_refs(
    s2: S2Client | OpenAlexClient, graph: Graph, seed: dict[str, Any], refs: list[dict[str, Any]]
) -> None:
    """Add seed references Semantic Scholar failed to link, from the seed's own bibliography."""
    linked = {
        r[0] for r in graph.db.execute(
            "SELECT n.norm_title FROM edges e JOIN nodes n ON n.pid=e.dst WHERE e.src=?", (seed["pid"],)
        )
    }
    added = 0
    for ref in refs:
        if norm_title(ref["title"]) in linked:
            continue
        node = await s2.match_title(ref["title"])
        if not node or norm_title(node["title"]) != norm_title(ref["title"]):
            logger.info("seed ref not in S2: {}", ref["title"][:60])
            continue
        graph.upsert_node(node, seed["depth"] + 1)
        graph.add_edge(seed["pid"], node["paperId"], {"contexts": [], "intents": [], "influential": False})
        added += 1
    graph.db.commit()
    logger.info("backfilled {} seed references from the bibliography", added)


def _source(run_dir: Path, graph: Graph, requested: str, s2_key: str,
            openalex_key: str) -> S2Client | OpenAlexClient:
    """Semantic Scholar by default (better coverage, citing sentences); OpenAlex on request
    or when S2 stays down before the first node (see _fall_back_if_s2_down).
    A graph keeps the source it was built from: node ids differ, so a run never mixes them."""
    row = graph.db.execute("SELECT v FROM meta WHERE k='source'").fetchone()
    has_nodes = graph.db.execute("SELECT 1 FROM nodes LIMIT 1").fetchone()
    have = row[0] if row else ("s2" if has_nodes else "")
    if have and requested and have != requested:
        raise RuntimeError(f"graph.sqlite was built from {have}, not {requested}; move it aside to rebuild")
    source = have or requested or "s2"
    if source == "openalex" and not openalex_key:
        raise RuntimeError("source openalex needs RELWORK_OPENALEX_API_KEY")
    graph.db.execute("INSERT OR REPLACE INTO meta VALUES ('source', ?)", (source,))
    graph.db.commit()
    logger.info("citation source: {}", source)
    if source == "openalex":
        return OpenAlexClient(run_dir / "cache" / "openalex.sqlite", openalex_key)
    return S2Client(run_dir / "cache" / "s2.sqlite", s2_key)


async def _fall_back_if_s2_down(
    client: S2Client | OpenAlexClient, run_dir: Path, graph: Graph, requested: str,
    openalex_key: str, seeds: list[dict[str, Any]],
) -> S2Client | OpenAlexClient:
    """Start on OpenAlex when S2 stays down before the first node exists.

    Only here: node ids differ between sources, so a graph with nodes can never switch.
    An explicit --source is honored as is. A successful probe is cached for the crawl.
    """
    if not isinstance(client, S2Client) or requested or not openalex_key or not seeds:
        return client
    if graph.db.execute("SELECT 1 FROM nodes LIMIT 1").fetchone():
        return client
    client.max_attempts = S2_PROBE_ATTEMPTS
    await client.by_arxiv(seeds[0]["meta"]["arxiv_id"])
    client.max_attempts = S2Client.max_attempts
    if not client.gave_up:
        return client
    logger.warning("s2 unavailable after {} attempts; starting this graph on openalex", S2_PROBE_ATTEMPTS)
    await client.aclose()
    graph.db.execute("INSERT OR REPLACE INTO meta VALUES ('source', 'openalex')")
    graph.db.commit()
    return OpenAlexClient(run_dir / "cache" / "openalex.sqlite", openalex_key)


async def crawl(run_dir: Path, max_depth: int, s2_key: str, openalex_key: str = "",
                source: str = "") -> None:
    facets = FacetSet.model_validate(yaml.safe_load((run_dir / "facets.yaml").read_text()))
    seeds = yaml.safe_load((run_dir / "seeds.yaml").read_text())
    context_path = run_dir / "context.md"
    context = context_path.read_text() if context_path.exists() else ""
    (run_dir / "cache").mkdir(exist_ok=True)
    graph = Graph(run_dir / "graph.sqlite")
    s2 = _source(run_dir, graph, source, s2_key, openalex_key)
    s2 = await _fall_back_if_s2_down(s2, run_dir, graph, source, openalex_key, seeds)
    llm = LLM()
    valid = {f.id for f in facets.facets}
    brief = _facet_brief(facets)
    try:
        for s in seeds:
            node = await s2.by_arxiv(s["meta"]["arxiv_id"])
            if node is None:
                raise RuntimeError(f"seed not in the citation source: {s['meta']['arxiv_id']}")
            graph.upsert_node(node, 0, is_seed=True)
            graph.db.commit()
            seed = graph.nodes_where("WHERE pid=?", (node["paperId"],))[0]
            for direction in ("references", "citations"):
                await _expand(s2, graph, seed, direction, max_depth)
            await _backfill_seed_refs(s2, graph, seed, s["references"])

        for rnd in range(1, MAX_ROUNDS + 1):
            pending = graph.nodes_where(
                "WHERE is_seed=0 AND pid NOT IN (SELECT pid FROM triage) ORDER BY pub_date DESC"
            )
            logger.info("round {}: triage {}", rnd, len(pending))
            for n, it in await _batched(llm, TRIAGE_SYSTEM, f"FACETS:\n{brief}\n\nPAPERS:\n", pending,
                                        graph, TriageResult, TRIAGE_BATCH, 400, 4000):
                graph.db.execute("INSERT OR REPLACE INTO triage VALUES (?,?,?)",
                                 (n["pid"], it.facet if it.facet in valid else "none", rnd))
            graph.db.commit()

            to_judge = graph.nodes_where(
                "WHERE pid IN (SELECT pid FROM triage WHERE facet!='none') "
                "AND pid NOT IN (SELECT pid FROM judgments) ORDER BY pub_date DESC"
            )
            logger.info("round {}: judge {}", rnd, len(to_judge))
            head = f"FACETS:\n{brief}\n\nRESEARCH CONTEXT:\n{context}\n\nPAPERS:\n"
            accepted = 0
            for n, it in await _batched(llm, JUDGE_SYSTEM, head, to_judge, graph, JudgeResult,
                                        JUDGE_BATCH, 3000, 6000):
                facet = it.facet if it.facet in valid else "none"
                grade = max(0, min(3, int(it.grade))) if facet != "none" else 0
                graph.db.execute(
                    "INSERT OR REPLACE INTO judgments VALUES (?,?,?,?,?,?,?,?)",
                    (n["pid"], rnd, facet, json.dumps(it.secondary_facets), it.relation, grade,
                     it.reason, it.contrast),
                )
                accepted += grade >= 2
            graph.db.commit()
            logger.info("round {}: {} newly accepted", rnd, accepted)
            write_report(run_dir, graph, facets, context)

            # Expand along the researcher's priorities: low-priority facets are mapped
            # but never expanded; within the rest, higher priority, grade, then newest.
            prio = {f.id: PRIORITY_RANK.get(f.priority, 1) for f in facets.facets}
            cands = graph.db.execute(
                "SELECT n.pid, j.facet, j.grade, n.pub_date FROM nodes n JOIN judgments j USING(pid) "
                "WHERE (n.depth < ? OR (j.grade=3 AND n.depth < ?)) AND j.grade>=2 "
                "AND n.pid NOT IN (SELECT pid FROM expanded)",
                (max_depth, max_depth + 1),
            ).fetchall()
            cands = [c for c in cands if prio.get(c[1], 1) < PRIORITY_RANK["low"]]
            cands.sort(key=lambda c: (prio.get(c[1], 1), -c[2], _neg_date(c[3])))
            frontier = [graph.nodes_where("WHERE pid=?", (c[0],))[0] for c in cands[:EXPAND_PER_ROUND]]
            new = 0
            for n in frontier:
                for direction in ("references", "citations"):
                    new += await _expand(s2, graph, n, direction, max_depth)
            logger.info("round {}: expanded {} nodes -> {} new nodes", rnd, len(frontier), new)
            if not frontier or (accepted < MIN_NEW_ACCEPTED and new == 0):
                break
        write_report(run_dir, graph, facets, context)
    finally:
        await llm.aclose()
        await s2.aclose()


# ---- report ------------------------------------------------------------------


def _bands(today: date) -> list[tuple[str, str, str]]:
    m6, y1, y3 = today - timedelta(days=183), today - timedelta(days=365), today - timedelta(days=1095)
    return [
        ("최근 6개월", m6.isoformat(), today.isoformat()),
        ("6~12개월", y1.isoformat(), m6.isoformat()),
        ("1~3년", y3.isoformat(), y1.isoformat()),
        ("3년 이전", "0000", y3.isoformat()),
    ]


def write_report(run_dir: Path, graph: Graph, facets: FacetSet, context: str) -> None:
    q = graph.db.execute
    n_nodes = q("SELECT COUNT(*) FROM nodes").fetchone()[0]
    n_pass = q("SELECT COUNT(*) FROM triage WHERE facet!='none'").fetchone()[0]
    n_tri = q("SELECT COUNT(*) FROM triage").fetchone()[0]
    rows = q(
        "SELECT n.title,n.pub_date,n.venue,n.arxiv_id,n.depth,j.facet,j.relation,j.grade,j.reason,j.contrast,n.pid "
        "FROM judgments j JOIN nodes n ON n.pid=j.pid WHERE j.grade>=2 ORDER BY j.grade DESC, n.pub_date DESC"
    ).fetchall()
    lines = [
        "# 관련연구 맵 — 인용 그래프 탐색 결과", "",
        f"- 그래프 노드 {n_nodes}편 · 1차 거름 {n_tri}편 중 통과 {n_pass}편 · 채택(등급≥2) {len(rows)}편",
        f"- 갱신 {date.today()} (라운드마다 덮어씀 — 중단돼도 여기까지 결과)", "",
    ]

    def how(pid: str) -> str:
        r = q("SELECT e.src, e.dst, n.title, n.depth FROM edges e JOIN nodes n ON n.pid = "
              "CASE WHEN e.src=? THEN e.dst ELSE e.src END WHERE (e.src=? OR e.dst=?) "
              "ORDER BY n.depth LIMIT 1", (pid, pid, pid)).fetchone()
        if not r:
            return ""
        return f"← 인용함: {r[2][:40]}" if r[0] == pid else f"← 인용됨: {r[2][:40]}"

    cell = lambda s: (s or "").replace("|", "/").replace("\n", " ")  # noqa: E731
    for name, start, end in _bands(date.today()) + [("날짜 미상", "", "")]:
        brows = [r for r in rows if ((start <= (r[1] or "") <= end) if start else not r[1])]
        lines += [f"## {name} — {len(brows)}편", ""]
        for f in facets.facets:
            frows = [r for r in brows if r[5] == f.id]
            if not frows:
                continue
            lines += [f"### {f.id} {f.name} [{f.priority}]", "",
                      "| 등급 | 논문 | 날짜 | 홉 | 그래프 연결 | 판단 이유 | 우리 아이디어와 차이 |",
                      "|---|---|---|---|---|---|---|"]
            for title, pub, venue, aid, depth, _, rel, grade, reason, contrast, pid in frows:
                link = f"[{title}](https://arxiv.org/abs/{aid})" if aid else title
                lines.append(f"| {grade} | {cell(link)} | {pub} | {depth} | {cell(how(pid))} | "
                             f"{cell(reason)} | {cell(contrast)} |")
            lines.append("")

    lines += ["## 재현율 점검 — 맥락(context.md)에 적힌 논문을 그래프에서 다시 찾았나", "",
              "| 논문 | 그래프에 있음 | 홉 | 판정 등급 |", "|---|---|---|---|"]
    seen: set[str] = set()
    for line in context.splitlines():
        for m in re.finditer(r"\d{4}\.\d{4,5}", line):
            aid = m.group(0)
            if aid in seen:
                continue
            seen.add(aid)
            name = re.split(r"[(:—]", line[: m.start()].split("·")[-1])[0].strip(" -*#").strip()
            name = re.sub(r"^(정독 완료|시드)\s*", "", name).split("/")[0].strip()
            r = q("SELECT n.depth, j.grade FROM nodes n LEFT JOIN judgments j ON j.pid=n.pid "
                  "WHERE n.arxiv_id=? OR (?!='' AND (n.title=? OR n.title LIKE ? OR n.title LIKE ?))",
                  (aid, name, name, f"{name}:%", f"{name} %")).fetchone()
            label = f"{name[:40]} ({aid})"
            lines.append(f"| {cell(label)} | {'O' if r else 'X'} | {r[0] if r else '-'} | "
                         f"{r[1] if r and r[1] is not None else '-'} |")
    (run_dir / "report.md").write_text("\n".join(lines) + "\n")
    logger.info("wrote {}", run_dir / "report.md")
