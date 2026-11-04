import asyncio

import httpx
import pytest

from relwork import graph as graph_module
from relwork.graph import Graph, S2Client, _fall_back_if_s2_down
from relwork.openalex import OpenAlexClient

SEEDS = [{"meta": {"arxiv_id": "2307.03172"}}]


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    real_sleep = asyncio.sleep

    async def fast(_seconds):
        await real_sleep(0)

    monkeypatch.setattr(graph_module.asyncio, "sleep", fast)
    monkeypatch.setattr(graph_module, "S2_PROBE_ATTEMPTS", 3)


def _client(tmp_path, status):
    client = S2Client(tmp_path / "s2.sqlite")
    client._interval = 0
    client.calls = 0

    def handler(_request):
        client.calls += 1
        return httpx.Response(status, json={"paperId": "p1"})

    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client


def _run(client, tmp_path, requested="", key="k", seeds=SEEDS):
    (tmp_path / "cache").mkdir()
    graph = Graph(tmp_path / "graph.sqlite")
    result = asyncio.run(_fall_back_if_s2_down(client, tmp_path, graph, requested, key, seeds))
    source = graph.db.execute("SELECT v FROM meta WHERE k='source'").fetchone()
    return result, source[0] if source else None


def test_switches_to_openalex_when_s2_stays_down(tmp_path):
    client = _client(tmp_path, 429)
    result, source = _run(client, tmp_path)
    assert isinstance(result, OpenAlexClient)
    assert source == "openalex"
    assert client.calls == 3


def test_stays_on_s2_when_it_answers(tmp_path):
    client = _client(tmp_path, 200)
    result, _ = _run(client, tmp_path)
    assert result is client
    assert client.max_attempts == S2Client.max_attempts


def test_missing_paper_is_not_an_outage(tmp_path):
    client = _client(tmp_path, 404)
    result, _ = _run(client, tmp_path)
    assert result is client


@pytest.mark.parametrize("kwargs", [{"requested": "s2"}, {"key": ""}, {"seeds": []}])
def test_no_probe_when_switch_is_not_allowed(tmp_path, kwargs):
    client = _client(tmp_path, 429)
    result, _ = _run(client, tmp_path, **kwargs)
    assert result is client
    assert client.calls == 0


def test_graph_with_nodes_never_switches(tmp_path):
    graph = Graph(tmp_path / "graph.sqlite")
    columns = [row[1] for row in graph.db.execute("PRAGMA table_info(nodes)")]
    required = [row for row in graph.db.execute("PRAGMA table_info(nodes)") if row[3]]
    values = {row[1]: "x" for row in required}
    values.setdefault("pid", "p0")
    assert "pid" in columns
    graph.db.execute(
        f"INSERT INTO nodes ({','.join(values)}) VALUES ({','.join('?' * len(values))})",
        tuple(values.values()),
    )
    graph.db.commit()
    client = _client(tmp_path, 429)
    result = asyncio.run(_fall_back_if_s2_down(client, tmp_path, graph, "", "k", SEEDS))
    assert result is client
    assert client.calls == 0
