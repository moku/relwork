"""Stage 1: read seed papers and extract a structured profile."""

from __future__ import annotations

from pathlib import Path

import yaml
from loguru import logger

from relwork.llm import LLM
from relwork.models import FacetSet, ReferenceList, SeedProfile
from relwork.sources import SeedMeta, fetch_pdf_text, http_client, resolve_seed, split_references

PROFILE_SYSTEM = """You are a careful research analyst reading a paper in full.
Extract only what the paper actually says; never invent benchmarks, baselines or citations.
Write descriptive fields in Korean (개조식, 짧게). Keep paper/method/benchmark names in English.
For related_work_groups, follow the paper's own Related Work section structure and list every
citation label it cites there, with the role the authors give it."""

REFS_SYSTEM = """You parse a paper's bibliography into structured entries.
Copy titles exactly as printed. Do not skip entries and do not invent any.
If an arXiv id or DOI is printed, copy it; otherwise leave it empty."""

FACETS_SYSTEM = """You design the axes (facets) along which related work should be mapped for a
researcher. You get the seed paper(s) and, when present, the researcher's CONTEXT: their own
research goal, idea, papers found so far, decisions and open questions. The researcher wants the
related work of THEIR research, using the seed as a starting point, not a literature review of
the seed for its own sake.

A facet is a dimension along which other papers can be related (same problem with a different
method, same mechanism applied to a different problem, a baseline family, an evaluation
benchmark, a direct competitor of the researcher's idea, ...).

Two kinds of facets:
- origin=seed: axes of the seed's own contribution, grounded in the seed profile and the seed's
  related-work grouping; cite anchor reference labels from the seed.
- origin=context: axes where the researcher's idea departs from the seed (its mechanism, its
  direct competitors, its open questions). These may have no anchor in the seed bibliography;
  anchor them on papers named in the context (anchor_known) and rely on search_queries.
  Every core element of the researcher's idea must be covered by some facet.

Rules:
- 4 to 8 facets in total, non-overlapping, each specific enough that a reader of an abstract can
  decide membership. Avoid vague axes like "multimodal AI".
- priority reflects relevance to the researcher's context: what they call low interest is low;
  axes of their own idea and its direct competitors are high. Without a context, judge by the seed.
- include/exclude must be concrete; exclude should name the near-misses.
- search_queries are English keyword queries for scholarly search engines.
Write Korean for name/description/include/exclude/rationale."""


async def build_seed(run_dir: Path, spec: str, llm: LLM) -> dict[str, object]:
    cache = run_dir / "cache"
    async with http_client() as client:
        meta: SeedMeta = await resolve_seed(client, spec)
        text = await fetch_pdf_text(client, meta.arxiv_id, cache)
    body, refs_text = split_references(text)
    logger.info(
        "seed {} body={} chars refs={} chars", meta.arxiv_id, len(body), len(refs_text)
    )
    header = f"Title: {meta.title}\nPublished: {meta.published}\nAbstract: {meta.abstract}\n"
    profile = await llm.structured(
        PROFILE_SYSTEM, f"{header}\n--- FULL TEXT ---\n{body}", SeedProfile, thinking=True
    )
    refs = await llm.structured(
        REFS_SYSTEM, f"--- BIBLIOGRAPHY ---\n{refs_text}", ReferenceList, max_tokens=24000
    )
    return {
        "meta": meta.model_dump(),
        "profile": profile.model_dump(),
        "references": [r.model_dump() for r in refs.references],
    }


async def build_facets(
    seeds: list[dict[str, object]], context: str, llm: LLM
) -> FacetSet:
    parts = []
    for seed in seeds:
        parts.append(
            yaml.safe_dump(
                {"meta": seed["meta"], "profile": seed["profile"], "references": seed["references"]},
                allow_unicode=True,
                sort_keys=False,
            )
        )
    user = "=== SEED ===\n" + "\n\n=== SEED ===\n".join(parts)
    if context.strip():
        user += f"\n\n=== RESEARCHER CONTEXT ===\n{context}"
    return await llm.structured(FACETS_SYSTEM, user, FacetSet, thinking=True)
