import discord
from discord import app_commands
from discord.ext import commands
import asyncio
import motor.motor_asyncio
import os
import datetime
import re

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
counters_db = db["counters"]

confession_queue = asyncio.Queue()
worker_started = False

DEFAULT_DELAY = 5
DAILY_LIMIT = 3
COOLDOWN_SECONDS = 15

BAD_WORDS = ["fuck", "shit", "bitch", "asshole", "nigger", "slut"]

# ================= GLOBAL COUNTER =================

async def generate_confession_id():
    counter = await counters_db.find_one_and_update(
        {"_id": "confession_id"},
        {"$inc": {"value": 1}},
        upsert=True,
        return_document=True
    )
    if not counter or "value" not in counter:
        doc = await counters_db.find_one({"_id": "confession_id"})
        return doc["value"]
    return counter["value"]

# ================= AUTOMOD =================

async def run_automod(message: str):
    lower = message.lower()

    if "discord.gg/" in lower or "discord.com/invite" in lower:
        return True, "Invites are not allowed."

    if "http://" in lower or "https://" in lower:
        return True, "Links are not allowed."

    if any(word in lower for word in BAD_WORDS):
        return True, "Inappropriate language detected."

    if len(message) > 15:
        caps_ratio = sum(c.isupper() for c in message) / len(message)
        if caps_ratio > 0.7:
            return True, "Too many caps."

    if "@everyone" in message or "@here" in message:
        return True, "Mass mentions blocked."

    return False, None

# ================= WORKER =================

async def confession_worker():
    await bot.wait_until_ready()
    print("Confession worker started.")

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

            active_servers = 0
            success_servers = 0
            failed_servers = 0

            async for guild_data in settings_db.find({}):
                active_servers += 1
                guild_id = guild_data.get("guild_id")
                channel_id = guild_data.get("channel_id")

                print(f"\nChecking guild_id: {guild_id}")

                guild = bot.get_guild(guild_id)
                if not guild:
                    print("❌ Guild not found in bot cache. Skipping.")
                    failed_servers += 1
                    continue

                channel = guild.get_channel(channel_id)
                if not channel:
                    print(f"❌ Channel not found in {guild.name}. Removing broken config.")
                    await settings_db.delete_one({"guild_id": guild_id})
                    failed_servers += 1
                    continue

                # Permission check
                perms = channel.permissions_for(guild.me)
                if not perms.send_messages or not perms.embed_links:
                    print(f"❌ Missing permissions in {guild.name}.")
                    failed_servers += 1
                    continue

                try:
                    msg = await channel.send(embed=embed)
                    await msg.add_reaction("👍")
                    await msg.add_reaction("👎")

                    print(f"✅ Sent confession #{confession_id} to {guild.name}")
                    success_servers += 1

                except discord.Forbidden:
                    print(f"❌ Forbidden in {guild.name}. Removing config.")
                    await settings_db.delete_one({"guild_id": guild_id})
                    failed_servers += 1

                except discord.HTTPException as e:
                    print(f"❌ HTTP error in {guild.name}: {e}")
                    failed_servers += 1

                except Exception as e:
                    print(f"❌ Unexpected error in {guild.name}: {e}")
                    failed_servers += 1

                await asyncio.sleep(1)

            print("\n====== BROADCAST SUMMARY ======")
            print(f"Active servers: {active_servers}")
            print(f"Successful sends: {success_servers}")
            print(f"Failed sends: {failed_servers}")
            print("================================\n")

            await asyncio.sleep(DEFAULT_DELAY)

        except Exception as e:
            print(f"Worker fatal error: {e}")
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

    if len(message) > 1500:
        await interaction.followup.send("Message too long.", ephemeral=True)
        return

    banned = await bans_db.find_one({
        "guild_id": guild_id,
        "user_id": user_id
    })

    if banned:
        await interaction.followup.send("You are banned.", ephemeral=True)
        return

    now = datetime.datetime.utcnow()
    cooldown = await cooldown_db.find_one({
        "guild_id": guild_id,
        "user_id": user_id
    })

    if cooldown:
        diff = (now - cooldown["last_used"]).total_seconds()
        if diff < COOLDOWN_SECONDS:
            await interaction.followup.send(
                f"Cooldown active. Try again in {int(COOLDOWN_SECONDS - diff)}s.",
                ephemeral=True
            )
            return

    today = now.date().isoformat()

    count = await confessions_db.count_documents({
        "user_id": user_id,
        "date": today
    })

    if count >= DAILY_LIMIT:
        await interaction.followup.send("Daily limit reached.", ephemeral=True)
        return

    blocked, reason = await run_automod(message)
    if blocked:
        await interaction.followup.send(reason, ephemeral=True)
        return

    confession_id = await generate_confession_id()

    await confessions_db.insert_one({
        "confession_id": confession_id,
        "user_id": user_id,
        "guild_origin": guild_id,
        "message": message,
        "date": today
    })

    await cooldown_db.update_one(
        {"guild_id": guild_id, "user_id": user_id},
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

# ================= BAN SYSTEM =================

@tree.command(name="banconfess", description="Ban user from confessing")
@app_commands.checks.has_permissions(administrator=True)
async def banconfess(interaction: discord.Interaction, user: discord.Member):

    await bans_db.update_one(
        {"guild_id": interaction.guild.id, "user_id": user.id},
        {"$set": {"user_id": user.id}},
        upsert=True
    )

    await interaction.response.send_message("User banned from confessing.")

@tree.command(name="unbanconfess", description="Unban user")
@app_commands.checks.has_permissions(administrator=True)
async def unbanconfess(interaction: discord.Interaction, user: discord.Member):

    await bans_db.delete_one({
        "guild_id": interaction.guild.id,
        "user_id": user.id
    })

    await interaction.response.send_message("User unbanned.")

# ================= STATS =================

@tree.command(name="confessionstats", description="View global stats")
async def confessionstats(interaction: discord.Interaction):

    total = await confessions_db.count_documents({})
    servers = await settings_db.count_documents({})

    embed = discord.Embed(
        title="Global Confession Network Stats",
        color=discord.Color.blue()
    )

    embed.add_field(name="Total Confessions", value=str(total))
    embed.add_field(name="Active Servers", value=str(servers))

    await interaction.response.send_message(embed=embed)

# ================= RUN =================

bot.run(TOKEN)
