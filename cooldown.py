import time
from database import cooldowns

async def check_cooldown(user_id: int, guild_id: int, cooldown_time: int):
    record = await cooldowns.find_one({
        "user_id": user_id,
        "guild_id": guild_id
    })

    current_time = int(time.time())

    if record:
        last_used = record["timestamp"]
        if current_time - last_used < cooldown_time:
            remaining = cooldown_time - (current_time - last_used)
            return False, remaining

    await cooldowns.update_one(
        {"user_id": user_id, "guild_id": guild_id},
        {"$set": {
            "user_id": user_id,
            "guild_id": guild_id,
            "timestamp": current_time
        }},
        upsert=True
    )

    return True, 0
