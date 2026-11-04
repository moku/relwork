"""Command-line entry point.

    relwork new <run> --seed <arxiv id|url|title> [--seed ...]
    relwork seed <run>      # stage 1: read seeds -> seeds.yaml
    relwork facets <run>    # stage 2: seeds + context.md -> facets.yaml (human edits it)
    relwork crawl <run> [--depth 2]  # stage 3: citation-graph exploration -> report.md
    relwork enrich <run>    # venue/year + affiliations for accepted papers
    relwork export <run>    # accepted subgraph -> nodes.csv, edges.csv, graph.mmd, graph.html

context.md is free-form: research goal, idea, papers found so far, decisions, open
questions, low-interest areas. It steers facet design and later judgements.
"""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

import yaml
from loguru import logger

from relwork.enrich import enrich
from relwork.export import export
from relwork.graph import crawl
from relwork.llm import LLM
from relwork.seed import build_facets, build_seed

RUNS_DIR = Path(__file__).resolve().parent.parent / "runs"

CONTEXT_TEMPLATE = """# 연구 맥락

> 자유 형식. 축 생성과 이후 판단의 기준이 된다. 비워 두면 시드만 보고 축을 만든다.

## 연구 목적 / 핵심 아이디어

## 시드와의 차이

## 지금까지 찾은 자료

## 결정·가정

## 열린 질문

## 관심 낮음
"""


def _dump(path: Path, data: object) -> None:
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=100))


def cmd_new(run: str, seeds: list[str]) -> None:
    run_dir = RUNS_DIR / run
    run_dir.mkdir(parents=True, exist_ok=True)
    _dump(run_dir / "run.yaml", {"seeds": seeds})
    context_path = run_dir / "context.md"
    if not context_path.exists():
        context_path.write_text(CONTEXT_TEMPLATE)
    logger.info("created run {}", run_dir)


async def cmd_seed(run: str) -> None:
    run_dir = RUNS_DIR / run
    specs: list[str] = yaml.safe_load((run_dir / "run.yaml").read_text())["seeds"]
    llm = LLM()
    try:
        results = [await build_seed(run_dir, spec, llm) for spec in specs]
    finally:
        await llm.aclose()
    _dump(run_dir / "seeds.yaml", results)
    logger.info("wrote {}", run_dir / "seeds.yaml")


async def cmd_facets(run: str) -> None:
    run_dir = RUNS_DIR / run
    seeds = yaml.safe_load((run_dir / "seeds.yaml").read_text())
    context_path = run_dir / "context.md"
    context = context_path.read_text() if context_path.exists() else ""
    llm = LLM()
    try:
        facet_set = await build_facets(seeds, context, llm)
    finally:
        await llm.aclose()
    _dump(run_dir / "facets.yaml", facet_set.model_dump())
    logger.info("wrote {}", run_dir / "facets.yaml")


def render_images(run_dir: Path) -> None:
    """graph.png = network view of graph.html, graph_tree.png = discovery tree (mermaid).

    Needs node + a puppeteer install (RELWORK_PUPPETEER_DIR, e.g. a mermaid-cli dir).
    """
    import subprocess

    LLM()  # loads .env into os.environ
    pdir = os.environ.get("RELWORK_PUPPETEER_DIR", "")
    if not pdir:
        logger.info("RELWORK_PUPPETEER_DIR not set; skipping PNG rendering")
        return
    script = Path(__file__).resolve().parent.parent / "scripts" / "screenshot.cjs"
    subprocess.run(["node", str(script), pdir, str(run_dir / "graph.html"), str(run_dir / "graph.png")],
                   check=True, stdin=subprocess.DEVNULL)
    # mermaid's default limits replace large diagrams with an error image, and mmdc still exits 0.
    mmd_config = run_dir / "mermaid.config.json"
    mmd_config.write_text('{"maxTextSize": 10000000, "maxEdges": 100000}')
    subprocess.run([f"{pdir}/node_modules/.bin/mmdc", "-i", str(run_dir / "graph.mmd"), "-o",
                    str(run_dir / "graph_tree.png"), "-s", "2", "-b", "white", "-c", str(mmd_config)],
                   check=True, stdin=subprocess.DEVNULL, capture_output=True)
    logger.info("wrote {} and {}", run_dir / "graph.png", run_dir / "graph_tree.png")


def main() -> None:
    parser = argparse.ArgumentParser(prog="relwork")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_new = sub.add_parser("new")
    p_new.add_argument("run")
    p_new.add_argument("--seed", action="append", required=True)
    for name in ("seed", "facets"):
        sub.add_parser(name).add_argument("run")
    sub.add_parser("export").add_argument("run")
    sub.add_parser("enrich").add_argument("run")
    p_crawl = sub.add_parser("crawl")
    p_crawl.add_argument("run")
    p_crawl.add_argument("--depth", type=int, default=2)
    p_crawl.add_argument("--source", choices=["s2", "openalex"], default="",
                         help="citation source for a new graph (default s2; an existing graph keeps its own)")
    args = parser.parse_args()
    if args.cmd == "new":
        cmd_new(args.run, args.seed)
    elif args.cmd == "seed":
        asyncio.run(cmd_seed(args.run))
    elif args.cmd == "facets":
        asyncio.run(cmd_facets(args.run))
    elif args.cmd == "enrich":
        LLM()  # loads .env into os.environ
        asyncio.run(enrich(RUNS_DIR / args.run))
    elif args.cmd == "export":
        for path in export(RUNS_DIR / args.run).values():
            logger.info("wrote {}", path)
        render_images(RUNS_DIR / args.run)
    elif args.cmd == "crawl":
        LLM()  # loads .env into os.environ
        asyncio.run(crawl(RUNS_DIR / args.run, args.depth, os.environ.get("RELWORK_S2_API_KEY", ""),
                          os.environ.get("RELWORK_OPENALEX_API_KEY", ""), args.source))


if __name__ == "__main__":
    main()
