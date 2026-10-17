import json
import os

from dotenv import load_dotenv
from falkordb import FalkorDB

load_dotenv()

# 1. Database Credentials
CLOUD_HOST = os.getenv("FALKORDB_HOST", "r-6jissuruar.instance-gijzjhvh6.hc-7up0crkyn.ap-south-1.aws.f2e0a955bb84.cloud")
CLOUD_PORT = int(os.getenv("FALKORDB_PORT", 52078))
CLOUD_USERNAME = os.getenv("FALKORDB_USERNAME", "falkordb")
CLOUD_PASSWORD = os.getenv("FALKORDB_PASSWORD", "password")

# 2. Connect to database
db = FalkorDB(host=CLOUD_HOST, port=CLOUD_PORT, username=CLOUD_USERNAME, password=CLOUD_PASSWORD)
graph = db.select_graph("SigmaCompanyBrain")

# Tasks belong to a Discord server; tasks.json entries without guild_id use DISCORD_GUILD_ID
DEFAULT_GUILD_ID = os.getenv("DISCORD_GUILD_ID")

# 3. Load the JSON data
with open('tasks.json', 'r') as file:
    tasks_data = json.load(file)

for task in tasks_data:
    task['guild_id'] = task.get('guild_id') or DEFAULT_GUILD_ID
    if not task['guild_id']:
        raise SystemExit(f"Task {task['task_id']} has no guild_id and DISCORD_GUILD_ID is not set")

# Tasks ingested before servers were tracked belong to the default server
if DEFAULT_GUILD_ID:
    graph.query("MATCH (t:Task) WHERE t.guild_id IS NULL SET t.guild_id = $guild_id",
                {'guild_id': DEFAULT_GUILD_ID})

# 4. Ingest data into the graph (task IDs are unique per server, not globally)
for task in tasks_data:
    # Create Task and Developer nodes, and ASSIGNED_TO relationship
    query = """
    MERGE (t:Task {id: $task_id, guild_id: $guild_id})
    SET t.title = $title
    MERGE (d:Developer {name: $developer})
    MERGE (d)-[:ASSIGNED_TO]->(t)
    """
    graph.query(query, {'task_id': task['task_id'], 'guild_id': task['guild_id'],
                        'title': task['title'], 'developer': task['developer']})

# 5. Create BLOCKED_BY relationships within the same server
for task in tasks_data:
    if task['blocked_by']:
        query = """
        MATCH (t1:Task {id: $task_id, guild_id: $guild_id})
        MATCH (t2:Task {id: $blocked_by, guild_id: $guild_id})
        MERGE (t1)-[:BLOCKED_BY]->(t2)
        """
        graph.query(query, {'task_id': task['task_id'], 'blocked_by': task['blocked_by'],
                            'guild_id': task['guild_id']})

print("Graph ingestion complete!")
