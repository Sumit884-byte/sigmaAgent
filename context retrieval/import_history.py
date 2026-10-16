"""Import mock Discord history into Person 3's configured FalkorDB graph."""

from __future__ import annotations

import json
from pathlib import Path

from graph_store import connect_graph, import_messages
from models import DiscordMessage


def main() -> None:
    fixture = Path(__file__).with_name("mock_discord_history.json")
    raw_messages = json.loads(fixture.read_text(encoding="utf-8"))
    messages = [DiscordMessage.model_validate(item) for item in raw_messages]
    graph = connect_graph()

    # Seed only mock Task nodes from Person 2's shared fixture so this isolated
    # development graph can exercise explicit evidence-to-task links.
    task_fixture = fixture.parent.parent / "tasks.json"
    if task_fixture.exists():
        for task in json.loads(task_fixture.read_text(encoding="utf-8")):
            graph.query(
                "MERGE (t:Task {id: $task_id}) SET t.title = $title",
                {"task_id": task["task_id"], "title": task["title"]},
            )

    counts = import_messages(graph, messages)
    print(
        "Imported {messages} messages, {evidence} evidence items, "
        "and {task_links} explicit task links.".format(**counts)
    )


if __name__ == "__main__":
    main()
