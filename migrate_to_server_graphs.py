"""Split the shared SigmaCompanyBrain graph into one FalkorDB graph per Discord server.

Each server's graph starts as a GRAPH.COPY of the shared graph, then everything that
belongs to other servers is deleted. The shared graph is left untouched as a backup.

    python migrate_to_server_graphs.py            # creates <FALKORDB_GRAPH_PREFIX><guild_id>
    python migrate_to_server_graphs.py --prefix Scratch_   # dry run into throwaway graphs
"""
import argparse
import os

from dotenv import load_dotenv
from falkordb import FalkorDB

load_dotenv()

SOURCE_GRAPH = "SigmaCompanyBrain"

# Order matters: later steps remove nodes left orphaned by earlier ones
PRUNE_QUERIES = [
    "MATCH (t:Task) WHERE t.guild_id IS NULL OR t.guild_id <> $g DETACH DELETE t",
    "MATCH (e:TaskChange) WHERE e.guild_id IS NULL OR e.guild_id <> $g DETACH DELETE e",
    "MATCH (c:DiscordChannel) WHERE c.guild_id IS NULL OR c.guild_id <> $g DETACH DELETE c",
    # Synced messages hang off a channel; messages recorded by task changes carry guild_id
    """MATCH (m:DiscordMessage)
       WHERE NOT (m)-[:IN_CHANNEL]->(:DiscordChannel) AND coalesce(m.guild_id, '') <> $g
       DETACH DELETE m""",
    "MATCH (e:ContextEvidence) WHERE NOT (:DiscordMessage)-[:SUPPORTS]->(e) DETACH DELETE e",
    """OPTIONAL MATCH (c:DiscordChannel)
       WITH collect(c.id) AS channel_ids
       MATCH (s:DiscordSyncState) WHERE NOT s.channel_id IN channel_ids
       DETACH DELETE s""",
    """MATCH (a:DiscordAuthor)
       WHERE NOT (a)-[:AUTHORED]->(:DiscordMessage) AND NOT (:TaskChange)-[:MADE_BY]->(a)
       DETACH DELETE a""",
    """MATCH (d:Developer)
       WHERE NOT (d)-[:ASSIGNED_TO]->(:Task) AND NOT (:DiscordAuthor)-[:MAPS_TO]->(d)
         AND NOT (:TaskChange)-[:AFFECTS_DEVELOPER]->(d)
       DETACH DELETE d""",
]


def server_ids(graph):
    rows = graph.ro_query("""
    MATCH (n) WHERE (n:Task OR n:DiscordChannel OR n:TaskChange) AND n.guild_id IS NOT NULL
    RETURN DISTINCT n.guild_id
    """).result_set
    return sorted(str(row[0]) for row in rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--prefix", default=os.getenv("FALKORDB_GRAPH_PREFIX", "Sigma_"))
    parser.add_argument("--replace", action="store_true", help="overwrite server graphs that already exist")
    args = parser.parse_args()

    db = FalkorDB(host=os.environ["FALKORDB_HOST"], port=int(os.environ["FALKORDB_PORT"]),
                  username=os.getenv("FALKORDB_USERNAME"), password=os.getenv("FALKORDB_PASSWORD"))
    existing = set(db.list_graphs())
    for guild_id in server_ids(db.select_graph(SOURCE_GRAPH)):
        target = f"{args.prefix}{guild_id}"
        if target in existing:
            if not args.replace:
                print(f"{target}: already exists, skipping (use --replace to overwrite)")
                continue
            db.select_graph(target).delete()
        db.connection.execute_command("GRAPH.COPY", SOURCE_GRAPH, target)
        graph = db.select_graph(target)
        for query in PRUNE_QUERIES:
            graph.query(query, {"g": guild_id})
        counts = graph.ro_query("MATCH (n) RETURN labels(n)[0], count(n) ORDER BY 1").result_set
        print(f"{target}: " + ", ".join(f"{label} {count}" for label, count in counts))


if __name__ == "__main__":
    main()
