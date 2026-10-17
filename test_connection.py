import os

from dotenv import load_dotenv
from falkordb import FalkorDB

load_dotenv()

# Database Credentials
CLOUD_HOST = os.getenv("FALKORDB_HOST", "r-6jissuruar.instance-gijzjhvh6.hc-7up0crkyn.ap-south-1.aws.f2e0a955bb84.cloud")
CLOUD_PORT = int(os.getenv("FALKORDB_PORT", 52078))
CLOUD_USERNAME = os.getenv("FALKORDB_USERNAME", "falkordb")
CLOUD_PASSWORD = os.getenv("FALKORDB_PASSWORD", "password")

# Connect to database
try:
    db = FalkorDB(host=CLOUD_HOST, port=CLOUD_PORT, username=CLOUD_USERNAME, password=CLOUD_PASSWORD)
    graph = db.select_graph(os.getenv("FALKORDB_GRAPH", "SigmaCompanyBrain"))
    graph.query("RETURN 1")  # Force a round trip so bad credentials fail here
    print("Successfully connected to FalkorDB Cloud!")
except Exception as e:
    print(f"Connection failed: {e}")
