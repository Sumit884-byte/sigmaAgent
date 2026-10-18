from __future__ import annotations

import os
import unittest
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import falkordb
from fastapi.testclient import TestClient

# /investigate requires the internal key, like the bot and the website proxy send
TEST_INVESTIGATION_KEY = "test-investigation-key"
AUTH_HEADERS = {"Authorization": f"Bearer {TEST_INVESTIGATION_KEY}"}


def use_test_investigation_key(test_case):
    patcher = patch.dict(os.environ, {"INVESTIGATION_API_KEY": TEST_INVESTIGATION_KEY})
    patcher.start()
    test_case.addCleanup(patcher.stop)


class QueryResult:
    def __init__(self, result_set):
        self.result_set = result_set


class RecordingGraph:
    """Small stateful graph fake for testing the HTTP-to-audit path."""

    def __init__(self):
        self.tasks = [["T5", "Build LLM Agent", "Person 3", None, "todo"]]
        self.events = []
        self.queries = []
        self.dependencies = []
        self.report_rows = []

    def ro_query(self, query, params=None):
        return self.query(query, params)

    def query(self, query, params=None):
        params = params or {}
        self.queries.append((query, params))
        if "RETURN task.id, blocker.id" in query:
            return QueryResult([edge.copy() for edge in self.dependencies])
        if "RETURN event.summary LIMIT 1" in query:
            event = next((event for event in self.events if event["id"] == params["event_id"]), None)
            return QueryResult([[event["summary"]]] if event else [])
        if "MATCH (event:TaskChange {guild_id: $guild_id, action: 'assign'})" in query:
            events = [event for event in self.events
                      if event["guild_id"] == params["guild_id"] and event["action"] == "assign"
                      and event["task_id"] in params["task_ids"]]
            return QueryResult([[event["task_id"], event.get("developer_name"), event["actor_name"],
                                 event["timestamp"], event["source_url"]] for event in events])
        if "RETURN t.id, t.title, d.name, b.title, t.status" in query:
            return QueryResult([row.copy() for row in self.tasks])
        if "RETURN t.id, t.title, coalesce(t.status, 'todo'), owners" in query:
            return QueryResult([row.copy() for row in self.report_rows])
        if "MATCH (t:Task {guild_id: $g}) RETURN t.id" in query:
            return QueryResult([[row[0]] for row in self.tasks])
        if "WHERE toLower(d.name) = toLower($name)" in query:
            return QueryResult([])
        if "MATCH (t:Task {id: $id, guild_id: $g}) RETURN t.id" in query:
            return QueryResult([[params["id"]]] if any(row[0] == params["id"] for row in self.tasks) else [])
        if "RETURN count(*)" in query and "BLOCKED_BY*1.." in query:
            return QueryResult([[0]])
        if "RETURN t.id\n" in query:
            return QueryResult([[row[0]] for row in self.tasks])
        if "MATCH (event:TaskChange {guild_id: $guild_id})" in query:
            events = [event for event in self.events if event["guild_id"] == params["guild_id"]]
            if params.get("task_id"):
                events = [event for event in events if event["task_id"] == params["task_id"]]
            if params.get("action"):
                events = [event for event in events if event["action"] == params["action"]]
            if params.get("developer_name"):
                events = [event for event in events if event.get("developer_name") == params["developer_name"]]
            events.sort(key=lambda event: event["timestamp"], reverse=True)
            return QueryResult([[
                event["action"], event["task_id"], event["task_title"], event["summary"],
                event["actor_name"], event["actor_id"], event["timestamp"], event["source_url"],
                event.get("developer_name"), event["details"],
            ] for event in events[:50]])
        if "MERGE (event:TaskChange" in query:
            if params["event_action"] == "create":
                self.tasks.append([params["id"], params["title"], None, None, "todo"])
            task = next(row for row in self.tasks if row[0] == params["id"])
            event = {
                "id": params["event_id"], "guild_id": params["g"],
                "action": params["event_action"], "task_id": params["id"],
                "task_title": task[1],
                "summary": params["event_summary"], "actor_name": params["actor_name"],
                "actor_id": params["actor_id"], "timestamp": params["source_timestamp"],
                "source_url": params["source_url"],
                "developer_name": params.get("event_developer_name"),
                "details": params["event_details"],
            }
            self.events = [old for old in self.events if old["id"] != event["id"]]
            self.events.append(event)
            if params["event_action"] == "assign":
                task[2] = params["event_developer_name"]
            elif params["event_action"] == "unassign":
                task[2] = None
            elif params["event_action"] == "rename":
                task[1] = params["title"]
            elif params["event_action"] == "set_status":
                task[4] = params["status"]
            elif params["event_action"] == "delete":
                self.tasks.remove(task)
            return QueryResult([])
        return QueryResult([])


_original_falkordb = falkordb.FalkorDB
_import_graph = RecordingGraph()
with patch.object(falkordb, "FalkorDB", return_value=SimpleNamespace(select_graph=lambda _name: _import_graph)):
    import api


class ChangeProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.graph = RecordingGraph()
        # Every server's graph resolves to the recording fake
        self.original_tenant_graph = api.tenant_graph
        api.tenant_graph = lambda _guild_id: self.graph
        use_test_investigation_key(self)
        self.client = TestClient(api.app, headers=AUTH_HEADERS)
        self.discord = {
            "guild_id": "123456789012345678",
            "channel_id": "234567890123456789",
            "message_id": "345678901234567890",
            "author_id": "456789012345678901",
            "author_name": "Sumit",
            "timestamp": "2026-10-16T12:34:56+00:00",
            "message_content": "<@999999999999999999> assign T5 to Person 4",
        }

    def tearDown(self):
        api.tenant_graph = self.original_tenant_graph

    def test_discord_assignment_is_audited_and_why_answer_links_source(self):
        change_reply = api.AgentReply(
            answer="Assignment recorded.",
            changes=[api.TaskChange(action="assign", task_id="T5", developer="Person 4")],
        )
        with patch.object(api, "_gemini_client", return_value=object()), \
             patch.object(api, "_channel_history", return_value=[]), \
             patch.object(api, "_ask_gemini", return_value=change_reply):
            response = self.client.post("/investigate", json={
                "question": "assign T5 to Person 4", "discord": self.discord,
            })

        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("Assigned **T5** to **Person 4**", response.json()["answer"])
        audit_queries = [entry for entry in self.graph.queries if "MERGE (event:TaskChange" in entry[0]]
        self.assertEqual(len(audit_queries), 1)
        query, params = audit_queries[0]
        self.assertIn("MERGE (event)-[:TRIGGERED_BY]->(source)", query)
        self.assertIn("MERGE (event)-[:MADE_BY]->(actor)", query)
        self.assertIn("MERGE (event)-[:AFFECTS]->(t)", query)
        self.assertEqual(params["actor_name"], "Sumit")
        self.assertEqual(params["source_message_id"], self.discord["message_id"])
        self.assertEqual(
            params["source_url"],
            "https://discord.com/channels/123456789012345678/234567890123456789/345678901234567890",
        )
        self.assertEqual(params["source_timestamp"], "2026-10-16T12:34:56+00:00")

        with patch.object(api, "_gemini_client", return_value=None):
            why = self.client.post("/investigate", json={
                "question": "Why is T5 owned by Person 4?",
                "discord": {"guild_id": self.discord["guild_id"]},
            })
        self.assertEqual(why.status_code, 200, why.text)
        answer = why.json()["answer"]
        self.assertIn("Person 4", answer)
        self.assertIn("Sumit", answer)
        self.assertIn("Oct 16", answer)
        self.assertIn("[source message](https://discord.com/channels/", answer)

    def test_changes_without_discord_source_are_not_applied(self):
        change_reply = api.AgentReply(
            answer="I understood an assignment.",
            changes=[api.TaskChange(action="assign", task_id="T5", developer="Person 4")],
        )
        with patch.object(api, "_gemini_client", return_value=object()), \
             patch.object(api, "_channel_history", return_value=[]), \
             patch.object(api, "_ask_gemini", return_value=change_reply):
            response = self.client.post("/investigate", json={
                "question": "assign T5 to Person 4",
                "discord": {"guild_id": "123456789012345678"},
            })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("valid Discord message, channel, and author are required", response.json()["answer"])
        self.assertEqual(self.graph.events, [])
        self.assertEqual(self.graph.tasks[0][2], "Person 3")

    def test_explain_mode_returns_ordered_path_and_the_query_used(self):
        self.graph.tasks = [
            ["T5", "Build LLM Agent", "Person 4", "Build API", "todo"],
            ["T4", "Build API", "Person 2", "Plan API", "in_progress"],
            ["T3", "Plan API", "Person 3", None, "done"],
        ]
        self.graph.dependencies = [["T5", "T4"], ["T4", "T3"]]
        with patch.object(api, "_gemini_client", return_value=None):
            response = self.client.post("/investigate", json={
                "question": "explain T5", "discord": {"guild_id": self.discord["guild_id"]},
            })

        self.assertEqual(response.status_code, 200, response.text)
        answer = response.json()["answer"]
        self.assertIn("T5: Build LLM Agent (Person 4; todo) ← T4: Build API (Person 2; in_progress) ← T3: Plan API (Person 3; done)", answer)
        self.assertIn("Graph query used:", answer)
        self.assertIn("Assignment source query:", answer)
        self.assertIn("MATCH (task:Task", answer)
        self.assertNotIn("BLOCKED_BY*", answer)
        self.assertIn("MATCH (event:TaskChange", answer)
        self.assertIn('"guild_id": "123456789012345678"', answer)
        self.assertIn('"task_ids": ["T5", "T4", "T3"]', answer)
        query_calls = [(query, params) for query, params in self.graph.queries
                       if "RETURN task.id, blocker.id" in query]
        self.assertEqual(len(query_calls), 1)
        self.assertEqual(query_calls[0][1], {"guild_id": self.discord["guild_id"]})

    def test_replayed_create_change_does_not_create_another_task(self):
        provenance = api._discord_provenance(self.discord, self.discord["guild_id"], "Create docs task")
        change = api.TaskChange(action="create", title="Write API Docs")
        first = api._apply_change(self.discord["guild_id"], change, "Sumit", provenance, index=0)
        task_count = len(self.graph.tasks)
        replay = api._apply_change(self.discord["guild_id"], change, "Sumit", provenance, index=0)

        self.assertEqual(first, "Created **T6: Write API Docs**")
        self.assertEqual(replay, first)
        self.assertEqual(len(self.graph.tasks), task_count)
        self.assertEqual(len([event for event in self.graph.events if event["action"] == "create"]), 1)

    def test_explain_title_aggregates_duplicate_rows_and_all_owners(self):
        self.graph.tasks = [
            ["T5", "Build LLM Agent", "Person 4", "Build API", "todo"],
            ["T5", "Build LLM Agent", "Person 6", "Plan API", "todo"],
            ["T5", "Build LLM Agent", "Person 4", "Plan API", "todo"],
            ["T4", "Build API", "Person 2", None, "in_progress"],
            ["T3", "Plan API", "Person 3", None, "done"],
        ]
        self.graph.dependencies = [["T5", "T4"], ["T5", "T3"]]
        with patch.object(api, "_gemini_client", return_value=None):
            response = self.client.post("/investigate", json={
                "question": 'explain "Build LLM Agent"',
                "discord": {"guild_id": self.discord["guild_id"]},
            })
        answer = response.json()["answer"]
        self.assertIn("T5: Build LLM Agent (Person 4, Person 6; todo)", answer)
        self.assertEqual(answer.count("T5: Build LLM Agent"), 1)

    def test_explain_prose_question_is_not_captured_by_explain_mode(self):
        reply = api.AgentReply(answer="T5 is late because it is waiting on T4.", changes=[])
        with patch.object(api, "_gemini_client", return_value=object()), \
             patch.object(api, "_channel_history", return_value=[]), \
             patch.object(api, "_ask_gemini", return_value=reply) as ask:
            response = self.client.post("/investigate", json={
                "question": "explain why T5 is late",
                "discord": {"guild_id": self.discord["guild_id"]},
            })
        self.assertEqual(response.json()["answer"], reply.answer)
        ask.assert_called_once()

    def test_owner_why_uses_exact_title_and_includes_all_current_owners(self):
        rows = [
            ["T5", "API Integration", "Person 2", None, "todo"],
            ["T5", "API Integration", "Person 3", "Another API", "todo"],
            ["T6", "API Service", "Person 4", None, "todo"],
        ]
        guild_id = self.discord["guild_id"]
        self.assertIsNone(api._ownership_provenance_answer("Why is the project blocked?", guild_id, rows))
        for owner, msg_id, actor, month_day in [
            ("Person 2", "510000000000000001", "Sumit", "Oct 16"),
            ("Person 3", "510000000000000002", "Riya", "Oct 17"),
        ]:
            self.graph.events.append({
                "id": msg_id, "guild_id": self.discord["guild_id"], "action": "assign",
                "task_id": "T5", "task_title": "API Integration", "summary": "Assigned owner",
                "actor_name": actor, "actor_id": self.discord["author_id"],
                "timestamp": f"2026-10-{month_day[-2:]}T12:00:00+00:00",
                "source_url": f"https://discord.com/channels/g/c/{msg_id}",
                "developer_name": owner, "details": "{}",
            })
        answer = api._ownership_provenance_answer("Why is API Integration the owner?", guild_id, rows)
        self.assertIn("Person 2", answer)
        self.assertIn("Person 3", answer)
        self.assertIn("Sumit", answer)
        self.assertIn("Riya", answer)

    def test_task_change_context_is_fetched_only_for_relevant_why_question(self):
        rows = [["T5", "Build LLM Agent", "Person 3", None, "todo"]]
        self.graph.queries.clear()
        normal = api._relevant_task_change_context("What is T5 doing?", self.discord["guild_id"], rows)
        self.assertIn("no task-change history requested", normal)
        self.assertFalse(any("MATCH (event:TaskChange" in query for query, _ in self.graph.queries))

        why = api._relevant_task_change_context("Why is T5 blocked?", self.discord["guild_id"], rows)
        self.assertIn("no recorded changes", why)
        history_queries = [query for query, _ in self.graph.queries if "MATCH (event:TaskChange" in query]
        self.assertEqual(len(history_queries), 1)

    def test_dependency_report_contains_paths_without_global_audit_history(self):
        # Rows as returned by TASK_OVERVIEW_QUERY: ..., upstream, downstream, blocker chains
        self.graph.report_rows = [
            ["T1", "Frontend", "todo", ["Person 1"], [["T2", False], ["T3", False]],
             ["T2", "T3", "T4", "T5"], [],
             [["T1", "T3", "T4", "T5"], ["T1", "T2", "T4", "T5"], ["T1", "T2"], ["T1", "T3"]]],
            ["T2", "Auth", "todo", ["Person 2"], [["T4", False]], ["T4", "T5"], ["T1"], [["T2", "T4", "T5"]]],
            ["T3", "Backend", "todo", ["Person 3"], [["T4", False]], ["T4", "T5"], ["T1"], [["T3", "T4", "T5"]]],
            ["T4", "API", "todo", ["Person 4"], [["T5", False]], ["T5"], ["T1", "T2", "T3"], [["T4", "T5"]]],
            ["T5", "Database", "todo", ["Person 5"], [], [], ["T1", "T2", "T3", "T4"], []],
        ]
        self.graph.queries.clear()

        report = api._dependency_report(self.discord["guild_id"])

        self.assertIn("dependency path: T1 (Person 1) ← T2 (Person 2) ← T4 (Person 4) ← T5 (Person 5)", report)
        self.assertIn("if this slips, it also delays: T1 (Person 1), T2 (Person 2), T3 (Person 3), T4 (Person 4)",
                      report)
        # Only the read-only graph analysis queries run; no global audit history is fetched
        self.assertEqual([query for query, _ in self.graph.queries], [
            api.TASK_OVERVIEW_QUERY, api.CRITICAL_PATH_QUERY, api.BOTTLENECK_QUERY, api.WORKSTREAMS_QUERY,
        ])
        self.assertFalse(any("TaskChange" in query for query, _ in self.graph.queries))

    def test_every_mutation_action_writes_its_audit_event(self):
        self.graph.tasks.append(["T6", "Database Setup", "Person 2", None, "todo"])
        provenance = api._discord_provenance(self.discord, self.discord["guild_id"], "source text")
        changes = [
            api.TaskChange(action="create", title="New task"),
            api.TaskChange(action="assign", task_id="T5", developer="Person 4"),
            api.TaskChange(action="unassign", task_id="T5"),
            api.TaskChange(action="block", task_id="T5", blocked_by="T6"),
            api.TaskChange(action="unblock", task_id="T5", blocked_by="T6"),
            api.TaskChange(action="set_status", task_id="T5", status="in_progress"),
            api.TaskChange(action="rename", task_id="T5", title="Renamed task"),
            api.TaskChange(action="delete", task_id="T5"),
        ]
        for index, change in enumerate(changes):
            api._apply_change(
                self.discord["guild_id"], change, "Sumit", provenance, index=index,
            )

        audit_queries = [query for query, _ in self.graph.queries if "MERGE (event:TaskChange" in query]
        self.assertEqual(len(audit_queries), len(changes))
        expected_actions = [change.action for change in changes]
        self.assertEqual([event["action"] for event in self.graph.events], expected_actions)
        self.assertTrue(all("MERGE (event)-[:TRIGGERED_BY]->(source)" in query for query in audit_queries))


@unittest.skipUnless(os.getenv("RUN_PROVENANCE_INTEGRATION") == "1", "opt-in FalkorDB provenance integration")
class FalkorDbProvenanceIntegrationTests(unittest.TestCase):
    def test_change_query_persists_all_source_links_in_real_falkordb(self):
        graph_name = f"SigmaProvenanceTest{uuid.uuid4().hex[:10]}"
        graph = _original_falkordb(
            host=os.getenv("FALKORDB_HOST", "127.0.0.1"),
            port=int(os.getenv("FALKORDB_PORT", "6379")),
            username=os.getenv("FALKORDB_USERNAME") or None,
            password=os.getenv("FALKORDB_PASSWORD") or None,
        ).select_graph(graph_name)
        guild_id = "123456789012345678"
        task_id = "T5"
        developer_name = f"Person 4 {uuid.uuid4().hex[:6]}"
        source_message_id = str(500000000000000000 + uuid.uuid4().int % 300000000000000000)
        actor_id = str(800000000000000000 + uuid.uuid4().int % 100000000000000000)
        api.tenant_graph = lambda _guild_id: graph
        use_test_investigation_key(self)
        client = TestClient(api.app, headers=AUTH_HEADERS)
        try:
            graph.query(
                "CREATE (:Task {id: $id, guild_id: $g, title: 'Build LLM Agent', status: 'todo'})",
                {"id": task_id, "g": guild_id},
            )
            discord = {
                "guild_id": guild_id,
                "channel_id": "234567890123456789",
                "message_id": source_message_id,
                "author_id": actor_id,
                "author_name": "Sumit",
                "timestamp": "2026-10-16T12:34:56+00:00",
                "message_content": "Assign this task to Person 4.",
            }
            change_reply = api.AgentReply(
                answer="Assignment recorded.",
                changes=[api.TaskChange(action="assign", task_id=task_id, developer=developer_name)],
            )
            with patch.object(api, "_gemini_client", return_value=object()), \
                 patch.object(api, "_channel_history", return_value=[]), \
                 patch.object(api, "_ask_gemini", return_value=change_reply):
                for _ in range(2):
                    applied = client.post("/investigate", json={
                        "question": f"assign T5 to {developer_name}", "discord": discord,
                    })
                    self.assertEqual(applied.status_code, 200, applied.text)
                    self.assertIn("Assignment recorded", applied.json()["answer"])

            linked = graph.query("""
                MATCH (event:TaskChange {guild_id: $g, task_id: $task_id})-[:AFFECTS]->(task:Task)
                MATCH (event)-[:MADE_BY]->(actor:DiscordAuthor)
                MATCH (event)-[:TRIGGERED_BY]->(message:DiscordMessage)
                MATCH (event)-[:AFFECTS_DEVELOPER]->(developer:Developer)
                RETURN event.action, event.task_id, event.task_title, event.actor_name,
                       actor.discord_id, message.id, message.source_url, event.timestamp,
                       developer.name, event.summary
            """, {"g": guild_id, "task_id": task_id}).result_set
            self.assertEqual(len(linked), 1, linked)
            row = linked[0]
            self.assertEqual(row[0], "assign")
            self.assertEqual(row[1], task_id)
            self.assertEqual(row[3], "Sumit")
            self.assertEqual(row[4], actor_id)
            self.assertEqual(row[5], source_message_id)
            self.assertIn(source_message_id, row[6])
            self.assertEqual(row[7], "2026-10-16T12:34:56+00:00")
            self.assertEqual(row[8], developer_name)
            self.assertIn("Assigned", row[9])

            with patch.object(api, "_gemini_client", return_value=None):
                explanation = client.post("/investigate", json={
                    "question": f"Why is T5 owned by {developer_name}?",
                    "discord": {"guild_id": guild_id},
                })
            self.assertEqual(explanation.status_code, 200, explanation.text)
            answer = explanation.json()["answer"]
            self.assertIn("Sumit", answer)
            self.assertIn("Oct 16", answer)
            self.assertIn(source_message_id, answer)

            graph.query(
                "CREATE (:Task {id: 'T6', guild_id: $g, title: 'Database Setup', status: 'todo'})",
                {"g": guild_id},
            )
            remaining_changes = [
                api.TaskChange(action="unassign", task_id="T5"),
                api.TaskChange(action="block", task_id="T5", blocked_by="T6"),
                api.TaskChange(action="unblock", task_id="T5", blocked_by="T6"),
                api.TaskChange(action="set_status", task_id="T5", status="in_progress"),
                api.TaskChange(action="rename", task_id="T5", title="Renamed Agent"),
                api.TaskChange(action="delete", task_id="T6"),
                api.TaskChange(action="create", title="New Provenanced Task"),
            ]
            for index, change in enumerate(remaining_changes, start=1):
                provenance = api._discord_provenance(
                    discord, guild_id, "Provenance operation.",
                )
                first_result = api._apply_change(guild_id, change, "Sumit", provenance, index=index)
                if change.action == "create":
                    replay_result = api._apply_change(guild_id, change, "Sumit", provenance, index=index)
                    self.assertEqual(replay_result, first_result)
                    created_count = graph.query(
                        "MATCH (t:Task {guild_id: $g, title: 'New Provenanced Task'}) RETURN count(t)",
                        {"g": guild_id},
                    ).result_set[0][0]
                    self.assertEqual(created_count, 1)
            action_rows = graph.query(
                "MATCH (event:TaskChange {guild_id: $g}) RETURN event.action",
                {"g": guild_id},
            ).result_set
            self.assertEqual(
                sorted(row[0] for row in action_rows),
                sorted(["assign"] + [change.action for change in remaining_changes]),
            )
            source_link_counts = graph.query("""
                MATCH (event:TaskChange {guild_id: $g})
                OPTIONAL MATCH (event)-[:MADE_BY]->(actor:DiscordAuthor)
                OPTIONAL MATCH (event)-[:TRIGGERED_BY]->(message:DiscordMessage)
                RETURN count(event), count(actor), count(message)
            """, {"g": guild_id}).result_set[0]
            self.assertEqual(source_link_counts, [8, 8, 8])
        finally:
            graph.query("MATCH (n) DETACH DELETE n")


@unittest.skipUnless(os.getenv("RUN_PROVENANCE_INTEGRATION") == "1", "opt-in FalkorDB dependency-path integration")
class FalkorDbDependencyPathIntegrationTests(unittest.TestCase):
    def test_explain_mode_reads_real_path_and_includes_query(self):
        graph_name = f"SigmaPathTest{uuid.uuid4().hex[:10]}"
        graph = _original_falkordb(
            host=os.getenv("FALKORDB_HOST", "127.0.0.1"),
            port=int(os.getenv("FALKORDB_PORT", "6379")),
            username=os.getenv("FALKORDB_USERNAME") or None,
            password=os.getenv("FALKORDB_PASSWORD") or None,
        ).select_graph(graph_name)
        guild_id = "123456789012345678"
        api.tenant_graph = lambda _guild_id: graph
        use_test_investigation_key(self)
        client = TestClient(api.app, headers=AUTH_HEADERS)
        try:
            graph.query("""
                CREATE (t5:Task {id: 'T5', guild_id: $g, title: 'Build LLM Agent', status: 'todo'}),
                       (t4:Task {id: 'T4', guild_id: $g, title: 'Build API', status: 'in_progress'}),
                       (t3:Task {id: 'T3', guild_id: $g, title: 'Plan API', status: 'done'}),
                       (p4:Developer {name: 'Person 4'}),
                       (p2:Developer {name: 'Person 2'}),
                       (p3:Developer {name: 'Person 3'}),
                       (t5)-[:BLOCKED_BY]->(t4), (t4)-[:BLOCKED_BY]->(t3),
                       (p4)-[:ASSIGNED_TO]->(t5), (p2)-[:ASSIGNED_TO]->(t4),
                       (p3)-[:ASSIGNED_TO]->(t3)
            """, {"g": guild_id})
            discord = {
                "guild_id": guild_id,
                "channel_id": "234567890123456789",
                "message_id": "345678901234567890",
                "author_id": "456789012345678901",
                "author_name": "Sumit",
                "timestamp": "2026-10-16T12:34:56+00:00",
                "message_content": "Assign T5 to Person 4.",
            }
            api._apply_change(
                guild_id,
                api.TaskChange(action="assign", task_id="T5", developer="Person 4"),
                "Sumit", api._discord_provenance(discord, guild_id, discord["message_content"]),
            )
            response = client.post("/investigate", json={
                "question": "explain T5", "discord": {"guild_id": guild_id},
            })
            self.assertEqual(response.status_code, 200, response.text)
            answer = response.json()["answer"]
            self.assertIn("T5: Build LLM Agent (Person 4; todo) ← T4: Build API (Person 2; in_progress) ← T3: Plan API (Person 3; done)", answer)
            self.assertIn("Graph query used:", answer)
            self.assertIn('"guild_id": "123456789012345678"', answer)
            self.assertIn("Source trail:", answer)
            self.assertIn("assigned by **Sumit**", answer)
            self.assertIn("https://discord.com/channels/123456789012345678/234567890123456789/345678901234567890", answer)
            report = api._dependency_report(guild_id)
            self.assertIn("dependency path: T5 (Person 4) ← T4 (Person 2) ← T3 (Person 3)", report)
        finally:
            graph.query("MATCH (n) DETACH DELETE n")


class AnswerWithSourcesTests(unittest.TestCase):
    """Verify that every /investigate response carries structured sources and cypher_used."""

    # ------------------------------------------------------------------
    # Unit tests for the pure helpers (no HTTP)
    # ------------------------------------------------------------------

    def test_extract_sources_finds_markdown_links(self):
        text = (
            "See [PR #42](https://github.com/org/repo/pull/42) and "
            "[auth spec](https://github.com/org/repo/blob/main/auth.md)."
        )
        sources, cypher = api._extract_sources_and_cypher(text)
        self.assertIsNone(cypher)
        urls = [s["url"] for s in sources]
        self.assertIn("https://github.com/org/repo/pull/42", urls)
        self.assertIn("https://github.com/org/repo/blob/main/auth.md", urls)
        types = {s["url"]: s["type"] for s in sources}
        self.assertEqual(types["https://github.com/org/repo/pull/42"], "github")

    def test_extract_sources_deduplicates_urls(self):
        url = "https://discord.com/channels/1/2/3"
        text = f"[first]({url}) and [second]({url})"
        sources, _ = api._extract_sources_and_cypher(text)
        self.assertEqual(len(sources), 1)

    def test_extract_sources_finds_plain_url(self):
        # Use a URL that has no 'doc' / '.md' / 'github' / 'discord' token so it maps to 'external'
        text = "More info at https://example.com/help/page"
        sources, _ = api._extract_sources_and_cypher(text)
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]["url"], "https://example.com/help/page")
        self.assertEqual(sources[0]["type"], "external")

    def test_extract_cypher_block(self):
        text = (
            "Answer here.\n\nGraph query used:\n"
            "```cypher\nMATCH (t:Task) RETURN t\n```"
        )
        _, cypher = api._extract_sources_and_cypher(text)
        self.assertIsNotNone(cypher)
        self.assertIn("MATCH (t:Task)", cypher)

    def test_infer_source_type_discord(self):
        self.assertEqual(api._infer_source_type("https://discord.com/channels/1/2/3"), "discord")

    def test_infer_source_type_github(self):
        self.assertEqual(api._infer_source_type("https://github.com/org/repo"), "github")

    def test_infer_source_type_doc(self):
        self.assertEqual(api._infer_source_type("https://example.com/readme.md"), "doc")

    def test_infer_source_type_external(self):
        self.assertEqual(api._infer_source_type("https://jira.example.com/ticket/1"), "external")

    def test_format_investigate_response_structure(self):
        result = api._format_investigate_response(
            "See [the PR](https://github.com/x/y/pull/1).\n\n"
            "Graph query used:\n```cypher\nMATCH (n) RETURN n\n```"
        )
        self.assertIn("answer", result)
        self.assertIn("sources", result)
        self.assertIn("cypher_used", result)
        self.assertEqual(result["sources"][0]["type"], "github")
        self.assertIn("MATCH (n)", result["cypher_used"])

    # ------------------------------------------------------------------
    # Integration tests via the HTTP client
    # ------------------------------------------------------------------

    def setUp(self):
        self.graph = RecordingGraph()
        # Override the default task so Person 4 is already the owner (matching events)
        self.graph.tasks = [["T5", "Build LLM Agent", "Person 4", None, "todo"]]
        self.original_tenant_graph = api.tenant_graph
        api.tenant_graph = lambda _guild_id: self.graph
        use_test_investigation_key(self)
        self.client = TestClient(api.app, headers=AUTH_HEADERS)
        self.discord = {
            "guild_id": "123456789012345678",
            "channel_id": "234567890123456789",
            "message_id": "345678901234567890",
            "author_id": "456789012345678901",
            "author_name": "Sumit",
            "timestamp": "2026-10-16T12:34:56+00:00",
            "message_content": "<@999999999999999999> assign T5 to Person 4",
        }

    def tearDown(self):
        api.tenant_graph = self.original_tenant_graph

    def test_explain_response_has_cypher_used(self):
        """explain T5 must return cypher_used extracted from the answer."""
        self.graph.tasks = [
            ["T5", "Build LLM Agent", "Person 4", "Build API", "todo"],
            ["T4", "Build API", "Person 2", "Plan API", "in_progress"],
            ["T3", "Plan API", "Person 3", None, "done"],
        ]
        self.graph.dependencies = [["T5", "T4"], ["T4", "T3"]]
        with patch.object(api, "_gemini_client", return_value=None):
            response = self.client.post("/investigate", json={
                "question": "explain T5",
                "discord": {"guild_id": self.discord["guild_id"]},
            })
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("answer", data)
        self.assertIn("sources", data)
        self.assertIn("cypher_used", data)
        # The explain answer embeds a ```cypher block; it must be extracted
        self.assertIsNotNone(data["cypher_used"])
        self.assertIn("MATCH", data["cypher_used"])

    def test_ownership_provenance_response_has_cypher_and_discord_source(self):
        """Why is T5 owned by Person 4? must return a discord source item and cypher_used."""
        self.graph.events.append({
            "id": "510000000000000001",
            "guild_id": self.discord["guild_id"],
            "action": "assign",
            "task_id": "T5",
            "task_title": "Build LLM Agent",
            "summary": "Assigned",
            "actor_name": "Sumit",
            "actor_id": self.discord["author_id"],
            "timestamp": "2026-10-16T12:34:56+00:00",
            "source_url": (
                "https://discord.com/channels/"
                f"{self.discord['guild_id']}/"
                f"{self.discord['channel_id']}/"
                "510000000000000001"
            ),
            "developer_name": "Person 4",
            "details": "{}",
        })
        with patch.object(api, "_gemini_client", return_value=None):
            response = self.client.post("/investigate", json={
                "question": "Why is T5 owned by Person 4?",
                "discord": {"guild_id": self.discord["guild_id"]},
            })
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("sources", data)
        self.assertIn("cypher_used", data)
        # Must extract the discord source link that provenance answer embeds
        discord_sources = [s for s in data["sources"] if s["type"] == "discord"]
        self.assertTrue(
            len(discord_sources) >= 1,
            f"Expected at least one discord source; got {data['sources']}",
        )
        # Cypher must be present (provenance answer appends Graph query used block)
        self.assertIsNotNone(data["cypher_used"])
        self.assertIn("TaskChange", data["cypher_used"])

    def test_help_response_always_has_sources_and_cypher_keys(self):
        """Help text has no URLs so sources==[] and cypher_used is None, but keys exist."""
        response = self.client.post("/investigate", json={"question": "help"})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("answer", data)
        self.assertIn("sources", data)
        self.assertIn("cypher_used", data)
        self.assertIsInstance(data["sources"], list)

    def test_no_guild_response_has_all_three_keys(self):
        """Even the 'ask me in a server' error path returns the full structured payload."""
        with patch.object(api, "answer_github_question", return_value=None):
            response = self.client.post("/investigate", json={"question": "What is T5?"})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("answer", data)
        self.assertIn("sources", data)
        self.assertIn("cypher_used", data)

    def test_existing_tests_still_see_answer_key(self):
        """Backwards-compat: response.json()['answer'] must still be a non-empty string."""
        with patch.object(api, "_gemini_client", return_value=None):
            response = self.client.post("/investigate", json={
                "question": "explain T5",
                "discord": {"guild_id": self.discord["guild_id"]},
            })
        self.assertIsInstance(response.json()["answer"], str)
        self.assertTrue(len(response.json()["answer"]) > 0)


if __name__ == "__main__":
    unittest.main()

