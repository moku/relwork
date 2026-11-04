"""Export the accepted citation subgraph: nodes.csv / edges.csv / graph.mmd / graph.html.

Kept nodes = seeds + papers graded >= 2. Every accepted node was discovered from an
expanded node (a seed or another accepted paper), so the kept subgraph stays connected
to the seed. Edges are citations: ``from`` cites ``to``.
"""

from __future__ import annotations

import csv
import html
import json
import re
from pathlib import Path

import yaml

from relwork.graph import Graph
from relwork.models import FacetSet

PALETTE = ["#d1495b", "#edae49", "#00798c", "#30638e", "#66a182", "#8d6a9f", "#7a7a7a", "#c26a3d"]


def _short(title: str) -> str:
    head = re.split(r"[:：]", title)[0].strip()
    return head if len(head) <= 40 else head[:38] + "…"


def _wrap(text: str, width: int = 34) -> list[str]:
    lines, cur = [], ""
    for word in text.split():
        if cur and len(cur) + 1 + len(word) > width:
            lines.append(cur)
            cur = word
        else:
            cur = f"{cur} {word}".strip()
    return lines + ([cur] if cur else [])


def _meta(g: Graph) -> dict[str, tuple[str, int | None, list[str]]]:
    try:
        rows = g.db.execute("SELECT pid, venue, year, affiliations FROM paper_meta").fetchall()
    except Exception:  # noqa: BLE001 - enrich not run yet
        return {}
    return {pid: (venue, year, json.loads(affs)) for pid, venue, year, affs in rows}


def _venue_line(meta: tuple[str, int | None, list[str]] | None, pub: str) -> str:
    if not meta:
        return (pub or "")[:7]
    venue, year, _ = meta
    return f"{venue} {year or (pub or '')[:4]}".strip()


def _affil_line(meta: tuple[str, int | None, list[str]] | None, n: int = 3) -> str:
    affs = meta[2] if meta else []
    return ", ".join(affs[:n]) + (f" 외 {len(affs) - n}" if len(affs) > n else "")


def export(run_dir: Path) -> dict[str, Path]:
    facets = FacetSet.model_validate(yaml.safe_load((run_dir / "facets.yaml").read_text()))
    color = {f.id: PALETTE[i % len(PALETTE)] for i, f in enumerate(facets.facets)}
    fname = {f.id: f.name for f in facets.facets}
    g = Graph(run_dir / "graph.sqlite")
    meta = _meta(g)
    nodes = g.db.execute(
        "SELECT n.pid, n.title, n.pub_date, n.venue, n.arxiv_id, n.depth, n.is_seed, "
        "j.facet, j.grade, j.relation, j.reason FROM nodes n LEFT JOIN judgments j USING(pid) "
        "WHERE n.is_seed=1 OR j.grade>=2 ORDER BY n.depth, n.pub_date DESC"
    ).fetchall()
    keep = {r[0] for r in nodes}
    edges = [
        (s, d, json.loads(c), bool(inf))
        for s, d, c, inf in g.db.execute("SELECT src, dst, contexts, influential FROM edges")
        if s in keep and d in keep
    ]

    cited_by = {pid: 0 for pid in keep}
    for s_, d_, _, _ in edges:
        cited_by[d_] += 1

    # Discovery tree: each non-seed node hangs off one kept neighbour one hop closer
    # to the seed (preferring higher grade). Direction is the crawl direction.
    depth = {r[0]: r[5] for r in nodes}
    grade = {r[0]: (r[8] or (9 if r[6] else 0)) for r in nodes}
    parent: dict[str, tuple[str, str]] = {}
    for s, d, _, _ in edges:
        for child, par, kind in ((d, s, "레퍼"), (s, d, "피인용")):
            if depth[par] == depth[child] - 1:
                cur = parent.get(child)
                if cur is None or grade[par] > grade[cur[0]]:
                    parent[child] = (par, kind)

    out = {k: run_dir / f for k, f in
           [("nodes", "nodes.csv"), ("edges", "edges.csv"), ("mmd", "graph.mmd"), ("html", "graph.html")]}
    ids = {r[0]: f"n{i}" for i, r in enumerate(nodes)}
    title = {r[0]: r[1] for r in nodes}

    with out["nodes"].open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["id", "title", "date", "venue", "affiliations", "arxiv", "hop", "facet", "facet_name",
                    "grade", "relation", "reason"])
        for pid, t, pub, venue, aid, depth, seed, facet, grade, rel, reason in nodes:
            m = meta.get(pid)
            w.writerow([ids[pid], t, pub, _venue_line(m, pub), "; ".join(m[2]) if m else "", aid, depth,
                        "SEED" if seed else facet,
                        "시드" if seed else fname.get(facet, ""), grade if grade is not None else "",
                        rel or "", reason or ""])
    with out["edges"].open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["from", "to", "from_title", "to_title", "influential", "tree_edge", "context"])
        for s, d, ctx, inf in edges:
            tree = parent.get(d) == (s, "레퍼") or parent.get(s) == (d, "피인용")
            w.writerow([ids[s], ids[d], title[s], title[d], int(inf), int(tree), ctx[0] if ctx else ""])

    # Mermaid (static PNG for chat)
    lines = ['%%{init: {"flowchart": {"wrappingWidth": 420, "nodeSpacing": 12, "rankSpacing": 70}, "themeVariables": {"fontSize": "15px"}}}%%', "flowchart LR"]
    for pid, t, pub, _, _, depth, seed, facet, grade, _, _ in nodes:
        m = meta.get(pid)
        tag = "SEED" if seed else f"{facet} g{grade}"
        esc = lambda x: html.escape(x).replace(chr(34), "#quot;")  # noqa: E731
        parts = [f"<b>{esc(line)}</b>" for line in _wrap(t)]
        parts.append(esc(f"{_venue_line(m, pub)} · {tag}"))
        if cited_by[pid]:
            parts.append(f"<b>맵 안 피인용 {cited_by[pid]}</b>")
        if m and m[2]:
            parts += [f"<i>{esc(line)}</i>" for line in _wrap(_affil_line(m), 40)]
        label = "<br/>".join(parts)
        shape = f'(["{label}"])' if seed else (f'{{{{"{label}"}}}}' if grade == 3 else f'["{label}"]')
        lines.append(f"  {ids[pid]}{shape}")
        lines.append(f"  class {ids[pid]} {'seed' if seed else facet}")
    # Static image shows only the discovery tree (seed -> ... ); all edges are in edges.csv/html.
    for child, (par, kind) in parent.items():
        lines.append(f"  {ids[par]} -->|{kind}| {ids[child]}")
    lines.append("  classDef seed fill:#222,color:#fff,stroke:#000")
    for fid, c in color.items():
        lines.append(f"  classDef {fid} fill:{c},color:#fff,stroke:#333")
    out["mmd"].write_text("\n".join(lines) + "\n")

    # Interactive HTML (cytoscape + dagre): same discovery tree as the PNG by default,
    # all other citation edges can be toggled on.
    els = []
    for pid, t, pub, venue, aid, depth, seed, facet, grade, rel, reason in nodes:
        m = meta.get(pid)
        tag = "SEED" if seed else f"{facet} g{grade}"
        label_lines = _wrap(t) + [f"{_venue_line(m, pub)} · {tag}"]
        if cited_by[pid]:
            label_lines.append(f"◀ 맵 안 피인용 {cited_by[pid]}")
        if m and m[2]:
            label_lines += _wrap(_affil_line(m), 40)
        els.append({"data": {
            "id": ids[pid], "label": "\n".join(label_lines), "title": t, "date": pub or "",
            "short": f"{_short(t)}\n{_venue_line(m, pub)}",
            "venue": _venue_line(m, pub), "affs": ", ".join(m[2]) if m else "",
            "arxiv": aid or "", "hop": depth, "facet": "SEED" if seed else facet,
            "facetName": "시드" if seed else fname.get(facet, ""), "grade": grade or 0,
            "relation": rel or "", "reason": reason or "",
            "color": "#222" if seed else color.get(facet, "#999"), "seed": bool(seed),
            "citedBy": cited_by[pid],
        }})
    for child, (par, kind) in parent.items():
        els.append({"data": {"id": f"t{ids[child]}", "source": ids[par], "target": ids[child],
                             "kind": kind, "tree": True}})
    for i, (s, d, ctx, inf) in enumerate(edges):
        els.append({"data": {"id": f"e{i}", "source": ids[s], "target": ids[d], "kind": "인용",
                             "context": ctx[0] if ctx else "", "influential": inf, "tree": False}})
    legend = "".join(
        f'<span class="lg"><i style="background:{color[f.id]}"></i>{f.id} {html.escape(f.name)}</span>'
        for f in facets.facets
    )
    out["html"].write_text(_HTML.replace("__ELEMENTS__", json.dumps(els, ensure_ascii=False))
                           .replace("__LEGEND__", legend))
    return out


_HTML = """<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Related Work Map</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/cytoscape/3.30.2/cytoscape.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/dagre@0.8.5/dist/dagre.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/cytoscape-dagre@2.5.0/cytoscape-dagre.js"></script>
<style>
:root{--bg:#fafafa;--fg:#222;--panel:#fff;--line:#ddd}
@media (prefers-color-scheme:dark){:root{--bg:#16181c;--fg:#e6e6e6;--panel:#202328;--line:#3a3d44}}
body{margin:0;font-family:system-ui,sans-serif;background:var(--bg);color:var(--fg)}
#cy{position:absolute;top:44px;left:0;right:340px;bottom:0}
#side{position:absolute;top:44px;right:0;width:340px;bottom:0;overflow:auto;background:var(--panel);
  border-left:1px solid var(--line);padding:12px;box-sizing:border-box;font-size:13px;line-height:1.5}
header{height:44px;display:flex;align-items:center;gap:12px;padding:0 12px;border-bottom:1px solid var(--line);
  overflow-x:auto;white-space:nowrap;font-size:12px}
.lg i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:4px}
@media (max-width:700px){#cy{right:0;bottom:40%}#side{top:60%;width:100%;border-left:0;border-top:1px solid var(--line)}}
</style></head><body>
<header><b>Related Work Map</b><span class="lg"><i style="background:#222"></i>시드</span>__LEGEND__
<label><input type="radio" name="view" value="net" checked> 네트워크</label>
<label><input type="radio" name="view" value="tree"> 계보(발견 경로)</label>
<span>· 화살표 A→B = A가 B를 인용 · 원이 클수록 맵 안에서 많이 인용됨 · 육각형 = 직접 경쟁(등급3)</span></header>
<div id="cy"></div><div id="side">노드나 화살표를 누르면 상세가 보입니다.</div>
<script>
const els = __ELEMENTS__;
const cy = cytoscape({container: document.getElementById('cy'), elements: els, style: [
  {selector: 'node', style: {'background-color': 'data(color)', 'border-width': 1, 'border-color': '#333',
    width: 'mapData(citedBy, 0, 15, 22, 90)', height: 'mapData(citedBy, 0, 15, 22, 90)',
    label: 'data(short)', 'text-wrap': 'wrap', 'text-max-width': 150, 'font-size': 10,
    'text-valign': 'bottom', 'text-margin-y': 3, color: '#555'}},
  {selector: 'node[?seed]', style: {width: 70, height: 70, 'font-weight': 'bold'}},
  {selector: 'node[grade = 3]', style: {shape: 'hexagon', 'border-width': 3, 'border-color': '#000'}},
  {selector: 'edge', style: {width: 1, 'line-color': '#999', 'target-arrow-color': '#999',
    'target-arrow-shape': 'triangle', 'arrow-scale': 0.8, 'curve-style': 'bezier', opacity: 0.6}},
  {selector: 'edge[?influential]', style: {width: 2.5, opacity: 0.9}},
  {selector: 'node.treemode', style: {shape: 'round-rectangle', label: 'data(label)', width: 'label',
    height: 'label', padding: 10, 'text-valign': 'center', 'text-margin-y': 0, color: '#fff',
    'text-max-width': 260, 'font-size': 11}},
  {selector: 'edge.treeedge', style: {'curve-style': 'taxi', 'taxi-direction': 'horizontal',
    label: 'data(kind)', 'font-size': 9, 'text-background-color': '#ddd', 'text-background-opacity': 1,
    color: '#333', opacity: 1}},
  {selector: '.hidden', style: {display: 'none'}},
  {selector: '.dim', style: {opacity: 0.08}}]});
const treeEdges = cy.edges('[?tree]'), citeEdges = cy.edges('[!tree]');
treeEdges.addClass('treeedge');
function show(view) {
  if (view === 'tree') {
    cy.nodes().addClass('treemode');
    citeEdges.addClass('hidden'); treeEdges.removeClass('hidden');
    cy.elements(':visible').layout({name: 'dagre', rankDir: 'LR', nodeSep: 14, rankSep: 80}).run();
  } else {
    cy.nodes().removeClass('treemode');
    treeEdges.addClass('hidden'); citeEdges.removeClass('hidden');
    cy.elements(':visible').layout({name: 'cose', animate: false, idealEdgeLength: 110,
      nodeRepulsion: 9000, nodeOverlap: 20, gravity: 0.25, numIter: 2500, randomize: false}).run();
  }
  cy.fit(undefined, 30);
}
document.querySelectorAll('input[name=view]').forEach(r => r.onchange = e => show(e.target.value));
show('net');
const side = document.getElementById('side');
const esc = s => String(s).replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
cy.on('tap', 'node', e => {
  const d = e.target.data();
  cy.elements().addClass('dim'); e.target.closedNeighborhood().removeClass('dim');
  side.innerHTML = `<h3>${esc(d.title)}</h3><p>${esc(d.venue)} · ${esc(d.date)} · ${d.hop}홉</p><p>${esc(d.affs)}</p><p>맵 안에서 이 논문을 인용한 논문: ${d.citedBy}편</p>
    <p><b>${esc(d.facet)}</b> ${esc(d.facetName)} · 등급 ${d.grade} · ${esc(d.relation)}</p><p>${esc(d.reason)}</p>
    ${d.arxiv ? `<p><a href="https://arxiv.org/abs/${d.arxiv}" target="_blank">arXiv ${d.arxiv}</a></p>` : ''}`;
});
cy.on('tap', 'edge', e => {
  const d = e.target.data();
  side.innerHTML = `<p><b>${esc(cy.getElementById(d.source).data('title'))}</b><br>→ ${esc(d.kind)} →<br>
    <b>${esc(cy.getElementById(d.target).data('title'))}</b></p><p>${esc(d.context || '')}</p>`;
});
cy.on('tap', e => { if (e.target === cy) cy.elements().removeClass('dim'); });
</script></body></html>
"""
