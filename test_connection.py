from falkordb import FalkorDB

# Database Credentials
CLOUD_HOST = "r-6jissuruar.instance-gijzjhvh6.hc-7up0crkyn.ap-south-1.aws.f2e0a955bb84.cloud" 
CLOUD_PORT = 52078
CLOUD_USERNAME = "falkordb"                  
CLOUD_PASSWORD = "password"

# Connect to database
try:
    db = FalkorDB(host=CLOUD_HOST, port=CLOUD_PORT, username=CLOUD_USERNAME, password=CLOUD_PASSWORD)
graph = db.select_graph("SigmaCompanyBrain")
graph.query("RETURN 1") # <--- Add this line to force network validation
print("Successfully connected to FalkorDB Cloud!")

