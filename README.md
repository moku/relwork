# relwork

Build a **related-work map from 1–3 seed papers** by following the citation graph.
A local LLM reads the candidates against your own research context and keeps only what matters.

- **Citation graph only.** Candidates come from references and citing papers, never keyword or embedding search.
- **Your context steers it.** A free-form `context.md` (goal, idea, papers already found, low-interest areas) drives every judgment.
- **Recent papers first.** You can stop mid-crawl and still have the newest results.
- **Local judgment.** All triage and grading runs on your own OpenAI-compatible endpoint.

Output: `report.md`, `nodes.csv` / `edges.csv`, an interactive `graph.html`, and optional PNGs.

> Generated summaries and reports are currently written in **Korean**, because the prompts ask for it.

## Install

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/moku/relwork.git && cd relwork
uv sync
cp .env.example .env   # set RELWORK_LLM_BASE_URL and one citation-source key
```

**You need a citation-source key in practice.** Semantic Scholar is the default for its better coverage and citing sentences, but it rate-limits hard without a key. If it stays down for about 4 minutes before the first node exists and `RELWORK_OPENALEX_API_KEY` is set, the crawl starts on OpenAlex instead. A graph keeps its source once it has nodes, because paper ids differ between sources.

## Usage

```bash
R=.venv/bin/relwork
$R new my-run --seed 2305.10601        # arXiv id, URL, or title; repeat --seed up to 3 times
$EDITOR runs/my-run/context.md         # your research context (recommended)
$R seed my-run                         # read seeds -> seeds.yaml
$R facets my-run                       # propose facets -> facets.yaml; review and edit priorities
$R crawl my-run --depth 2              # explore the citation graph -> report.md
                                       # --source s2|openalex fixes the source, with no automatic switch
$R enrich my-run                       # add venue, year and affiliations
$R export my-run                       # nodes.csv, edges.csv, graph.mmd, graph.html
```

Run `crawl` again with the same run to resume from its cache. List already-known papers in `context.md` with their arXiv ids so the report can compute recall.

## How it decides

- Each candidate is triaged, then graded 0–3 against your facets and context.
- Only papers graded 2 or higher are expanded, and low-priority facets are not expanded. Grade 3 papers get one extra hop.
- Expanding a paper takes all its references and up to 1,000 citing papers.
- **Known limit:** it can miss papers connected only through giant hubs (GPT-4 and the like), or sitting behind an intermediate paper that was filtered out.

## Claude Code skill

```bash
ln -s "$PWD/skill" ~/.claude/skills/relwork
```

The skill assumes the repo lives at `~/relwork`.

## Data sources

Semantic Scholar (default), OpenAlex, and arXiv. Please follow their terms and rate limits.

## License

MIT
