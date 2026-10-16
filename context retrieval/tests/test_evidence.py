from __future__ import annotations

import unittest

from evidence import explicit_task_reference, extract_evidence
from models import DiscordMessage


def make_message(content: str, message_id: str = "m1") -> DiscordMessage:
    return DiscordMessage(
        id=message_id,
        channel_id="c1",
        author_name="Developer",
        content=content,
        source_url="https://discord.com/channels/g1/c1/m1",
    )


class EvidenceExtractionTests(unittest.TestCase):
    def test_extracts_blocker_and_decision_with_exact_source_sentences(self) -> None:
        message = make_message(
            "T2 is blocked by a missing API key. Decision: use staging for the test."
        )
        evidence = extract_evidence(message)

        self.assertEqual([item.kind for item in evidence], ["blocker_candidate", "decision_candidate"])
        self.assertEqual(evidence[0].excerpt, "T2 is blocked by a missing API key.")
        self.assertEqual(evidence[0].source_url, message.source_url)

    def test_ignores_sentences_without_explicit_blocker_or_decision_language(self) -> None:
        self.assertEqual(extract_evidence(make_message("The dashboard colors look good.")), [])

    def test_task_link_requires_explicit_id_or_full_title(self) -> None:
        tasks = [{"id": "T2", "title": "Build LLM Agent"}]

        self.assertEqual(explicit_task_reference(make_message("T2 is waiting for access."), tasks), "T2")
        self.assertEqual(explicit_task_reference(make_message("Build LLM Agent is blocked."), tasks), "T2")
        self.assertIsNone(explicit_task_reference(make_message("The integration is blocked."), tasks))

    def test_task_mentioned_elsewhere_in_message_does_not_link_unrelated_evidence(self) -> None:
        message = make_message("Decision: use staging. T2 can resume after access is confirmed.")
        decision = extract_evidence(message)[0]

        self.assertIsNone(
            explicit_task_reference(message, [{"id": "T2", "title": "Build LLM Agent"}], decision.excerpt)
        )


if __name__ == "__main__":
    unittest.main()
