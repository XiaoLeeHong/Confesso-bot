import os
from motor.motor_asyncio import AsyncIOMotorClient

# Get Mongo URL from environment variable
MONGO_URL = os.getenv("MONGO_URL")

if not MONGO_URL:
    raise ValueError("MONGO_URL environment variable is not set.")

# Create client
client = AsyncIOMotorClient(MONGO_URL)

# Database
db = client["confesso"]

# Collections
guilds = db["guilds"]
cooldowns = db["cooldowns"]
