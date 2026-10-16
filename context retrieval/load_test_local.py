"""Run a bounded synthetic import/retrieval load against an isolated graph."""

from __future__ import annotations

import argparse
import os
import time

from graph_store import connect_graph, import_messages, retrieve_evidence
from models import DiscordMessage


def configure_load_graph(graph_name: str) -> None:
    if not graph_name.startswith("SigmaPerson3LoadTest"):
        raise ValueError("load-test graph must start with SigmaPerson3LoadTest")
    os.environ["FALKORDB_GRAPH"] = graph_name


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--messages", type=int, default=10_000)
    parser.add_argument("--channel", default="synthetic-load-test")
    parser.add_argument("--graph", default="SigmaPerson3LoadTest")
    args = parser.parse_args()
    if not 1 <= args.messages <= 50_000:
        parser.error("--messages must be between 1 and 50000")
    try:
        configure_load_graph(args.graph)
    except ValueError as exc:
        parser.error(str(exc))

    graph = connect_graph()
    graph.query("MERGE (t:Task {id: 'LOAD-T1'}) SET t.title = 'Synthetic load test task'")
    messages = []
    for index in range(args.messages):
        blocker = index % 10 == 0
        content = (
            f"LOAD-T1 is blocked by dependency loadtoken-{index} pending API access."
            if blocker else f"Synthetic server update {index}: routine status and progress."
        )
        messages.append(DiscordMessage(
            id=f"load-{index:08d}", channel_id=args.channel,
            channel_name="synthetic-load-test", guild_id="synthetic-guild",
            author_id=f"author-{index % 100:03d}",
            author_name=f"Synthetic Developer {index % 100:03d}",
            content=content,
            timestamp=f"2026-10-16T{(index // 3600) % 24:02d}:{(index // 60) % 60:02d}:{index % 60:02d}Z",
            source_url=f"https://discord.com/channels/synthetic-guild/{args.channel}/load-{index:08d}",
        ))

    start = time.perf_counter()
    counts = import_messages(graph, messages)
    import_seconds = time.perf_counter() - start

    start = time.perf_counter()
    results = retrieve_evidence(
        graph, terms=["blocked", "API", "access"],
        task_id="LOAD-T1", channel_id=args.channel, limit=20,
    )
    search_seconds = time.perf_counter() - start

    graph_counts = graph.query(
        "MATCH (m:DiscordMessage {channel_id: $channel}) WITH count(m) AS messages "
        "MATCH (e:ContextEvidence) RETURN messages, count(e)",
        {"channel": args.channel},
    ).result_set[0]
    expected_evidence = (args.messages + 9) // 10
    if counts["messages"] != args.messages or counts["evidence"] != expected_evidence:
        raise RuntimeError(f"Unexpected import counts: {counts}")
    if len(results) != min(20, expected_evidence) or not results[0]["source_url"].startswith("https://discord.com/"):
        raise RuntimeError("Indexed retrieval did not return the expected source-backed evidence")

    print(f"Graph: {os.getenv('FALKORDB_GRAPH', 'SigmaPerson3LoadTest')}")
    print(f"Imported: {counts['messages']:,} messages, {counts['evidence']:,} evidence, {counts['task_links']:,} task links")
    print(f"Graph records: {graph_counts[0]:,} channel messages; {graph_counts[1]:,} total evidence nodes")
    print(f"Import: {import_seconds:.2f}s ({args.messages / import_seconds:,.0f} messages/s)")
    print(f"Indexed retrieval: {search_seconds:.3f}s; {len(results)} results; source URL verified")
    print(f"Sample: {results[0]['evidence_excerpt']} ({results[0]['source_url']})")


if __name__ == "__main__":
    main()
