# Person 3 implementation checklist

1. **Use an isolated graph.** Configure Person 3's FalkorDB host, port, graph,
   and credentials with environment variables. Do not reuse Person 2's password
   or edit their root API/ingestion files.
2. **Use the shared schema.** Read existing `Task` nodes and `Developer` names
   from the graph. Add Discord-specific channel, message, and evidence nodes.
   Store Discord authors in `DiscordAuthor`, separately from Person 2's
   `Developer` records. Link an existing person only through the explicit
   `DISCORD_DEVELOPER_MAP` Discord-user-ID-to-developer-name setting; this adds
   a `MAPS_TO` relationship and preserves Person 2's canonical `name` field.
3. **Import history idempotently.** Import the mock fixture with
   `python import_history.py`. It copies Person 2's mock Task IDs/titles into
   the isolated test graph so task evidence can be linked locally, then imports
   mock Discord messages. For real history, sync through
   `POST /context/sync-channel`. Use stable Discord IDs and `MERGE` for nodes
   and edges so rerunning the import does not duplicate records.
4. **Extract only explicit evidence.** Detect clearly signaled blocker and
   decision sentences, retain their exact text, and connect each claim to its
   source message. Link evidence to a `Task` only when the message contains the
   task ID or full title.
5. **Retrieve cited context.** Use `POST /context/retrieve` with task fields
   supplied by Person 2 and return the original Discord URL, author, timestamp,
   source message, and extracted excerpt.
6. **Hand off to orchestration.** Person 4 combines Person 2's structured ticket
   facts with Person 3's source-linked conversation evidence. Person 3's
   service does not send messages or change tickets.
7. **Document and verify.** Follow the setup instructions in `README.md` and
   run `python -m unittest discover -s tests` for extraction, duplicate
   handling, and evidence retrieval behavior.
