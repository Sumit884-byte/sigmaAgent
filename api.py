from fastapi import FastAPI
from falkordb import FalkorDB

app = FastAPI(title="Sigma Agent API")

# 1. Database Credentials
CLOUD_HOST = "r-6jissuruar.instance-gijzjhvh6.hc-7up0crkyn.ap-south-1.aws.f2e0a955bb84.cloud" 
CLOUD_PORT = 52078
CLOUD_USERNAME = "falkordb"                  
CLOUD_PASSWORD = "AqpDbFC7hzL3"

# 2. Connect to database
db = FalkorDB(host=CLOUD_HOST, port=CLOUD_PORT, username=CLOUD_USERNAME, password=CLOUD_PASSWORD)
graph = db.select_graph("SigmaCompanyBrain")

# 3. Create the API Endpoint
@app.get("/dependencies")
def get_task_dependencies():
    # Query the graph for all blocking relationships
    query = """
    MATCH (t:Task)-[:BLOCKED_BY]->(b:Task)
    MATCH (d:Developer)-[:ASSIGNED_TO]->(b)
    RETURN t.title, b.title, d.name
    """
    result = graph.query(query)
    
    # Format the data for the AI agent
    dependencies = []
    for record in result.result_set:
        dependencies.append({
            "blocked_task": record[0],
            "waiting_on_task": record[1],
            "assigned_developer": record[2]
        })
        
    return {"status": "success", "data": dependencies}
