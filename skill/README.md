# relwork skill

Claude Code skill wrapper for the relwork CLI in this repo.

Install (once per machine):

    ln -s ~/relwork/skill ~/.claude/skills/relwork

Prerequisites: `uv sync` in `~/relwork` (creates `.venv`), `~/relwork/.env` with
`RELWORK_LLM_BASE_URL`, `RELWORK_LLM_API_KEY`, optional `RELWORK_S2_API_KEY`,
`RELWORK_PUPPETEER_DIR` (for PNG export).
