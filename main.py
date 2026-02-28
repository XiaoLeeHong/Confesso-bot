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
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

mongo = motor.motor_asyncio.AsyncIOMotorClient(MONGO_URL)
db = mongo["confession_bot"]

confessions_db = db["confessions"]
settings_db = db["settings"]
cooldown_db = db["cooldowns"]
config_db = db["config"]
logs_db = db["logs"]

confession_queue = asyncio.Queue()
DEFAULT_DELAY = 5
DAILY_LIMIT = 3
COOLDOWN_SECONDS = 15

# ================= GLOBAL CONFIG =================

async def get_global_delay():
    config = await config_db.find_one({"type": "global"})
    return config.get("delay", DEFAULT_DELAY) if config else DEFAULT_DELAY

async def set_global_delay(seconds: int):
    await config_db.update_one(
        {"type": "global"},
        {"$set": {"delay": seconds}},
        upsert=True
    )

# ================= WORKER =================

async def confession_worker():
    await bot.wait_until_ready()
    print("Confession worker started.")

    while True:
        data = await confession_queue.get()
        message = data["text"]

        delay = await get_global_delay()
        settings = settings_db.find({})

        async for guild_data in settings:
            try:
                guild = bot.get_guild(guild_data["guild_id"])
                if not guild:
                    continue

                channel = guild.get_channel(guild_data["channel_id"])
                if not channel:
                    continue

                embed = discord.Embed(
                    title="💜 Anonymous Confession",
                    description=message,
                    color=discord.Color.purple(),
                    timestamp=datetime.datetime.now(datetime.UTC)
                )
                embed.set_footer(text="Stay respectful.")

                await channel.send(embed=embed)

            except Exception as e:
                print(f"Error sending confession: {e}")

        await asyncio.sleep(delay)

# ================= EVENTS =================

@bot.event
async def on_ready():
    bot.loop.create_task(confession_worker())
    await tree.sync()
    print(f"Logged in as {bot.user}")

# ================= SETUP COMMANDS =================

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
        f"Confession channel set to {channel.mention}",
        ephemeral=True
    )

@tree.command(name="removesetup", description="Disable confessions")
@app_commands.checks.has_permissions(administrator=True)
async def removesetup(interaction: discord.Interation):
    await settings_db.delete_one({"guild_id": interaction.guild.id})
    await interaction.response.send_message(
        "Confession system disabled.",
        ephemeral=True
    )

@tree.command(name="config", description="View server config")
async def config(interaction: discord.Interaction):
    data = await settings_db.find_one({"guild_id": interaction.guild.id})

    if not data:
        await interaction.response.send_message(
            "Confessions not set up in this server.",
            ephemeral=True
        )
        return

    delay = await get_global_delay()
    channel = bot.get_channel(data["channel_id"])

    embed = discord.Embed(
        title="Confession Config",
        color=discord.Color.blurple()
    )
    embed.add_field(name="Channel", value=channel.mention if channel else "Not Found")
    embed.add_field(name="Global Delay", value=f"{delay}s")

    await interaction.response.send_message(embed=embed, ephemeral=True)

# ================= GLOBAL DELAY =================

@tree.command(name="setdelay", description="Set global confession delay")
@app_commands.checks.has_permissions(administrator=True)
async def setdelay(interaction: discord.Interaction, seconds: int):
    if seconds < 1:
        await interaction.response.send_message(
            "Delay must be at least 1 second.",
            ephemeral=True
        )
        return

    await set_global_delay(seconds)

    await interaction.response.send_message(
        f"Global delay set to {seconds}s",
        ephemeral=True
    )

# ================= CONFESSION =================

@tree.command(name="confess", description="Send anonymous confession")
async def confess(interaction: discord.Interaction, message: str):

    if len(message) > 1500:
        await interaction.response.send_message(
            "Confession too long. Keep it under 1500 characters.",
            ephemeral=True
        )
        return

    user_id = interaction.user.id
    today = datetime.date.today().isoformat()
    now = datetime.datetime.now(datetime.UTC)

    # DAILY LIMIT
    record = await confessions_db.find_one(
        {"user_id": user_id, "date": today}
    )

    if record and record["count"] >= DAILY_LIMIT:
        await interaction.response.send_message(
            f"You reached the daily limit of {DAILY_LIMIT}.",
            ephemeral=True
        )
        return

    # COOLDOWN
    cooldown = await cooldown_db.find_one({"user_id": user_id})
    if cooldown:
        last = cooldown["last_used"]
        if (now - last).total_seconds() < COOLDOWN_SECONDS:
            remaining = int(COOLDOWN_SECONDS - (now - last).total_seconds())
            await interaction.response.send_message(
                f"Cooldown active. Wait {remaining}s.",
                ephemeral=True
            )
            return

    # SWEAR FILTER (basic)
    banned_words = ["badword1", "badword2"]
    if any(word in message.lower() for word in banned_words):
        await interaction.response.send_message(
            "Message contains blocked words.",
            ephemeral=True
        )
        return

    # Update cooldown
    await cooldown_db.update_one(
        {"user_id": user_id},
        {"$set": {"last_used": now}},
        upsert=True
    )

    # Update daily count
    if record:
        await confessions_db.update_one(
            {"user_id": user_id, "date": today},
            {"$inc": {"count": 1}}
        )
    else:
        await confessions_db.insert_one(
            {"user_id": user_id, "date": today, "count": 1}
        )

    # Log confession internally
    await logs_db.insert_one({
        "user_id": user_id,
        "message": message,
        "timestamp": now
    })

    await confession_queue.put({"text": message})

    await interaction.response.send_message(
        "Your confession has been queued anonymously.",
        ephemeral=True
    )

# ================= STATS =================

@tree.command(name="stats", description="View global stats")
async def stats(interaction: discord.Interaction):

    total_confessions = await logs_db.count_documents({})
    total_servers = await settings_db.count_documents({})

    embed = discord.Embed(
        title="Global Confession Stats",
        color=discord.Color.green()
    )
    embed.add_field(name="Total Confessions Sent", value=str(total_confessions))
    embed.add_field(name="Servers Connected", value=str(total_servers))

    await interaction.response.send_message(embed=embed, ephemeral=True)

# ================= RUN =================

bot.run(TOKEN)
