import discord
from discord import app_commands
from discord.ext import commands
import asyncio
import motor.motor_asyncio
import os
import datetime

# ================= ENV =================

TOKEN = os.getenv("TOKEN")
MONGO_URL = os.getenv("MONGO_URL")

if not TOKEN or not MONGO_URL:
    raise Exception("TOKEN or MONGO_URL missing in environment variables.")

# ================= BOT SETUP =================

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.message_content = False

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

mongo = motor.motor_asyncio.AsyncIOMotorClient(MONGO_URL)
db = mongo["confession_bot"]

confessions_db = db["confessions"]
settings_db = db["settings"]
cooldown_db = db["cooldowns"]
logs_db = db["logs"]
bans_db = db["bans"]

confession_queue = asyncio.Queue()
worker_started = False

DEFAULT_DELAY = 5
DAILY_LIMIT = 3
COOLDOWN_SECONDS = 15

DEFAULT_AUTOMOD = {
    "banned_words": [],
    "block_links": True,
    "block_invites": True,
    "block_caps": True,
    "block_mentions": True,
    "manual_review": False,
    "review_channel_id": None
}

# ================= UTIL =================

async def get_guild_config(guild_id):
    config = await settings_db.find_one({"guild_id": guild_id})
    return config

async def generate_confession_id():
    count = await confessions_db.count_documents({})
    return count + 1

# ================= AUTOMOD =================

async def run_automod(guild_id: int, message: str):
    config = await get_guild_config(guild_id)
    if not config:
        return True, "System not configured."

    automod = config.get("automod", DEFAULT_AUTOMOD)
    lower_msg = message.lower()

    for word in automod.get("banned_words", []):
        if word in lower_msg:
            return True, "Blocked word detected."

    if automod.get("block_invites", True):
        if "discord.gg/" in lower_msg or "discord.com/invite" in lower_msg:
            return True, "Invites not allowed."

    if automod.get("block_links", True):
        if "http://" in lower_msg or "https://" in lower_msg:
            return True, "Links not allowed."

    if automod.get("block_caps", True):
        if len(message) > 15:
            caps_ratio = sum(c.isupper() for c in message) / len(message)
            if caps_ratio > 0.7:
                return True, "Too many caps."

    if automod.get("block_mentions", True):
        if "@everyone" in message or "@here" in message:
            return True, "Mass mentions blocked."

    return False, None

# ================= WORKER =================

async def confession_worker():
    await bot.wait_until_ready()

    while True:
        data = await confession_queue.get()
        guild_id = data["guild_id"]
        message = data["text"]
        confession_id = data["id"]

        config = await get_guild_config(guild_id)
        if not config:
            continue

        guild = bot.get_guild(guild_id)
        if not guild:
            continue

        channel = guild.get_channel(config["channel_id"])
        if not channel:
            continue

        embed = discord.Embed(
            title=f"💜 Anonymous Confession #{confession_id}",
            description=message,
            color=discord.Color.purple(),
            timestamp=datetime.datetime.now(datetime.UTC)
        )

        msg = await channel.send(embed=embed)
        await msg.add_reaction("👍")
        await msg.add_reaction("👎")

        await logs_db.insert_one({
            "guild_id": guild_id,
            "confession_id": confession_id,
            "message": message,
            "timestamp": datetime.datetime.utcnow()
        })

        delay = config.get("delay", DEFAULT_DELAY)
        await asyncio.sleep(delay)

# ================= EVENTS =================

@bot.event
async def on_ready():
    global worker_started
    if not worker_started:
        bot.loop.create_task(confession_worker())
        worker_started = True

    await tree.sync()
    print(f"Logged in as {bot.user}")

# ================= SETUP =================

@tree.command(name="setup", description="Set confession channel")
@app_commands.checks.has_permissions(administrator=True)
async def setup(interaction: discord.Interaction, channel: discord.TextChannel):

    await settings_db.update_one(
        {"guild_id": interaction.guild.id},
        {"$set": {
            "guild_id": interaction.guild.id,
            "channel_id": channel.id,
            "automod": DEFAULT_AUTOMOD,
            "delay": DEFAULT_DELAY
        }},
        upsert=True
    )

    await interaction.response.send_message("Confession channel set.", ephemeral=True)

# ================= AUTOMOD MANAGEMENT =================

@tree.command(name="addbannedword", description="Add banned word")
@app_commands.checks.has_permissions(administrator=True)
async def addbannedword(interaction: discord.Interaction, word: str):
    await settings_db.update_one(
        {"guild_id": interaction.guild.id},
        {"$push": {"automod.banned_words": word.lower()}}
    )
    await interaction.response.send_message("Word added.", ephemeral=True)

@tree.command(name="removebannedword", description="Remove banned word")
@app_commands.checks.has_permissions(administrator=True)
async def removebannedword(interaction: discord.Interaction, word: str):
    await settings_db.update_one(
        {"guild_id": interaction.guild.id},
        {"$pull": {"automod.banned_words": word.lower()}}
    )
    await interaction.response.send_message("Word removed.", ephemeral=True)

# ================= BAN SYSTEM =================

@tree.command(name="confessban", description="Ban user from confessing")
@app_commands.checks.has_permissions(administrator=True)
async def confessban(interaction: discord.Interaction, user: discord.User):
    await bans_db.update_one(
        {"guild_id": interaction.guild.id},
        {"$addToSet": {"banned_users": user.id}},
        upsert=True
    )
    await interaction.response.send_message("User banned from confessions.", ephemeral=True)

# ================= CONFESSION =================

@tree.command(name="confess", description="Send anonymous confession")
async def confess(interaction: discord.Interaction, message: str):

    guild_id = interaction.guild.id
    user_id = interaction.user.id

    if len(message) > 1500:
        await interaction.response.send_message("Too long.", ephemeral=True)
        return

    # Ban check
    banned = await bans_db.find_one({"guild_id": guild_id, "banned_users": user_id})
    if banned:
        await interaction.response.send_message("You are banned from confessing.", ephemeral=True)
        return

    # Cooldown
    cooldown = await cooldown_db.find_one({"guild_id": guild_id, "user_id": user_id})
    now = datetime.datetime.utcnow()

    if cooldown:
        last_used = cooldown["last_used"]
        diff = (now - last_used).total_seconds()
        if diff < COOLDOWN_SECONDS:
            await interaction.response.send_message(
                f"Cooldown active. Try again in {int(COOLDOWN_SECONDS - diff)}s.",
                ephemeral=True
            )
            return

    # Daily limit
    today = now.date().isoformat()
    count = await confessions_db.count_documents({
        "guild_id": guild_id,
        "user_id": user_id,
        "date": today
    })

    if count >= DAILY_LIMIT:
        await interaction.response.send_message("Daily limit reached.", ephemeral=True)
        return

    blocked, reason = await run_automod(guild_id, message)
    if blocked:
        await interaction.response.send_message(reason, ephemeral=True)
        return

    confession_id = await generate_confession_id()

    await confessions_db.insert_one({
        "guild_id": guild_id,
        "user_id": user_id,
        "message": message,
        "date": today,
        "confession_id": confession_id
    })

    await cooldown_db.update_one(
        {"guild_id": guild_id, "user_id": user_id},
        {"$set": {"last_used": now}},
        upsert=True
    )

    await confession_queue.put({
        "guild_id": guild_id,
        "text": message,
        "id": confession_id
    })

    await interaction.response.send_message("Confession queued anonymously.", ephemeral=True)

# ================= RUN =================

bot.run(TOKEN)
