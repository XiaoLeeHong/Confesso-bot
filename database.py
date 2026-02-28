from motor.motor_asyncio import AsyncIOMotorClient

MONGO_URL = "mongodb+srv://Lee:confess123@cluster0.cc6kqaf.mongodb.net/confesso?retryWrites=true&w=majority"

client = AsyncIOMotorClient(MONGO_URL)
db = client["confesso"]

guilds = db["guilds"]
cooldowns = db["cooldowns"]
