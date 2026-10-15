import json
from falkordb import FalkorDB

# 1. Database Credentials
CLOUD_HOST = "r-6jissuruar.instance-gijzjhvh6.hc-7up0crkyn.ap-south-1.aws.f2e0a955bb84.cloud" 
CLOUD_PORT = 52078
CLOUD_USERNAME = "falkordb"                  
CLOUD_PASSWORD = "password"

# 2. Connect to database
db = FalkorDB(host=CLOUD_HOST, port=CLOUD_PORT, username=CLOUD_USERNAME, password=CLOUD_PASSWORD)
graph = db.select_graph("SigmaCompanyBrain")

# 3. Load the JSON data
with open('tasks.json', 'r') as file:
    tasks_data = json.load(file)

# 4. Ingest data into the graph
for task in tasks_data:
    # Create Task and Developer nodes, and ASSIGNED_TO relationship
    query = """
    MERGE (t:Task {id: $task_id, title: $title})
    MERGE (d:Developer {name: $developer})
    MERGE (d)-[:ASSIGNED_TO]->(t)
    """
    graph.query(query, {'task_id': task['task_id'], 'title': task['title'], 'developer': task['developer']})

# 5. Create BLOCKED_BY relationships
for task in tasks_data:
    if task['blocked_by']:
        query = """
        MATCH (t1:Task {id: $task_id})
        MATCH (t2:Task {id: $blocked_by})
        MERGE (t1)-[:BLOCKED_BY]->(t2)
        """
        graph.query(query, {'task_id': task['task_id'], 'blocked_by': task['blocked_by']})

print("Graph ingestion complete!")
