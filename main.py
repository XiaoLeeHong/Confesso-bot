import discord
from discord import app_commands
from discord.ext import commands
import asyncio
import motor.motor_asyncio
import os
import datetime
import re
from pymongo import ReturnDocument

# ================= ENV =================

TOKEN = os.getenv("TOKEN")
MONGO_URL = os.getenv("MONGO_URL")

if not TOKEN or not MONGO_URL:
    raise Exception("TOKEN or MONGO_URL missing.")

# ================= BOT =================

intents = discord.Intents.default()
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

mongo = motor.motor_asyncio.AsyncIOMotorClient(MONGO_URL)
db = mongo["confession_bot"]

confessions_db = db["confessions"]
settings_db = db["settings"]
cooldown_db = db["cooldowns"]
bans_db = db["bans"]
global_bans_db = db["global_bans"]
counters_db = db["counters"]

confession_queue = asyncio.Queue()
worker_started = False

DEFAULT_DELAY = 3
DAILY_LIMIT = 3
COOLDOWN_SECONDS = 15

# Cleaner default list (no slurs hardcoded)
BAD_WORDS = ["fuck", "shit", "bitch", "asshole", "slut"]

# ================= GLOBAL COUNTER =================

async def generate_confession_id():
    counter = await counters_db.find_one_and_update(
        {"_id": "confession_id"},
        {"$inc": {"value": 1}},
        upsert=True,
        return_document=ReturnDocument.AFTER
    )
    return counter["value"]

# ================= AUTOMOD =================

async def run_automod(message: str):
    lower = message.lower()

    # Invite detection
    if re.search(r"(discord\.gg/|discord\.com/invite/)", lower):
        return True, "Invites are not allowed."

    # Basic domain detection
    if re.search(r"(https?://|www\.|\.com\b|\.net\b|\.org\b)", lower):
        return True, "Links are not allowed."

    # Word boundary profanity filter
    if any(re.search(rf"\b{re.escape(word)}\b", lower) for word in BAD_WORDS):
        return True, "Inappropriate language detected."

    # Caps spam
    if len(message) > 15:
        caps_ratio = sum(c.isupper() for c in message) / len(message)
        if caps_ratio > 0.7:
            return True, "Too many caps."

    return False, None

# ================= WORKER =================

async def confession_worker():
    await bot.wait_until_ready()
    print("Confession worker started.")

    semaphore = asyncio.Semaphore(10)

    while not bot.is_closed():
        try:
            data = await confession_queue.get()

            confession_id = data["id"]
            message = data["text"]

            embed = discord.Embed(
                title=f"🌍 Global Anonymous Confession #{confession_id}",
                description=message,
                color=discord.Color.purple(),
                timestamp=datetime.datetime.utcnow()
            )

            active = 0
            success = 0
            failed = 0
            tasks = []

            async for guild_data in settings_db.find({}):
                active += 1
                guild_id = guild_data.get("guild_id")
                channel_id = guild_data.get("channel_id")

                guild = bot.get_guild(guild_id)
                if not guild:
                    failed += 1
                    continue

                channel = guild.get_channel(channel_id)
                if not channel:
                    await settings_db.delete_one({"guild_id": guild_id})
                    failed += 1
                    continue

                perms = channel.permissions_for(guild.me)
                if not perms.send_messages or not perms.embed_links:
                    failed += 1
                    continue

                async def send(channel=channel, guild_name=guild.name):
                    nonlocal success, failed
                    async with semaphore:
                        try:
                            msg = await channel.send(embed=embed)
                            await msg.add_reaction("👍")
                            await msg.add_reaction("👎")
                            success += 1
                        except Exception:
                            failed += 1

                tasks.append(send())

            await asyncio.gather(*tasks)

            print(f"Broadcast #{confession_id} | Active:{active} Success:{success} Failed:{failed}")

            await asyncio.sleep(DEFAULT_DELAY)

        except Exception as e:
            print(f"Worker error: {e}")
            await asyncio.sleep(5)

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
            "channel_id": channel.id
        }},
        upsert=True
    )

    await interaction.response.send_message(
        "Server joined global confession network.",
        ephemeral=True
    )

# ================= CONFESS =================

@tree.command(name="confess", description="Send global anonymous confession")
async def confess(interaction: discord.Interaction, message: str):
    await interaction.response.defer(ephemeral=True)

    user_id = interaction.user.id
    guild_id = interaction.guild.id
    now = datetime.datetime.utcnow()
    today = now.date().isoformat()

    # Global ban check
    if await global_bans_db.find_one({"user_id": user_id}):
        await interaction.followup.send("You are globally banned.", ephemeral=True)
        return

    # Per guild ban
    if await bans_db.find_one({"guild_id": guild_id, "user_id": user_id}):
        await interaction.followup.send("You are banned in this server.", ephemeral=True)
        return

    # Cooldown (global)
    cooldown = await cooldown_db.find_one({"user_id": user_id})
    if cooldown:
        diff = (now - cooldown["last_used"]).total_seconds()
        if diff < COOLDOWN_SECONDS:
            await interaction.followup.send(
                f"Cooldown active. Try again in {int(COOLDOWN_SECONDS - diff)}s.",
                ephemeral=True
            )
            return

    # Daily limit (global model)
    count = await confessions_db.count_documents({
        "user_id": user_id,
        "date": today
    })

    if count >= DAILY_LIMIT:
        await interaction.followup.send("Daily limit reached.", ephemeral=True)
        return

    # Automod
    blocked, reason = await run_automod(message)
    if blocked:
        await confessions_db.insert_one({
            "user_id": user_id,
            "guild_origin": guild_id,
            "message": message,
            "date": today,
            "flagged": True,
            "reason": reason
        })
        await interaction.followup.send(reason, ephemeral=True)
        return

    # Escape mentions
    message = discord.utils.escape_mentions(message)

    confession_id = await generate_confession_id()

    await confessions_db.insert_one({
        "confession_id": confession_id,
        "user_id": user_id,
        "guild_origin": guild_id,
        "message": message,
        "date": today,
        "flagged": False
    })

    await cooldown_db.update_one(
        {"user_id": user_id},
        {"$set": {"last_used": now}},
        upsert=True
    )

    await confession_queue.put({
        "id": confession_id,
        "text": message
    })

    await interaction.followup.send(
        "Your confession was sent globally.",
        ephemeral=True
    )

bot.run(TOKEN)
