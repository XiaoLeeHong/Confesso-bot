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

confession_queue = asyncio.Queue()
DEFAULT_DELAY = 5


# ================= GLOBAL CONFIG =================

async def get_global_delay():
    config = await config_db.find_one({"type": "global"})
    if config:
        return config.get("delay", DEFAULT_DELAY)
    return DEFAULT_DELAY


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

    while not bot.is_closed():
        data = await confession_queue.get()
        confession_text = data["text"]

        delay = await get_global_delay()

        print("Broadcasting confession...")

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
                    description=confession_text,
                    color=discord.Color.purple(),
                    timestamp=datetime.datetime.now(datetime.UTC)
                )

                await channel.send(embed=embed)

            except Exception as e:
                print("Error sending to guild:", e)

        await asyncio.sleep(delay)


@bot.event
async def on_ready():
    bot.loop.create_task(confession_worker())
    await tree.sync()
    print(f"Logged in as {bot.user}")


# ================= SETUP COMMANDS =================

@tree.command(name="setup", description="Set confession channel")
@app_commands.checks.has_permissions(administrator=True)
async def setup(interaction: discord.Interaction, channel: discord.TextChannel):
    await interaction.response.defer()

    await settings_db.update_one(
        {"guild_id": interaction.guild.id},
        {
            "$set": {
                "guild_id": interaction.guild.id,
                "channel_id": channel.id
            }
        },
        upsert=True
    )

    await interaction.followup.send(
        f"Confession channel set to {channel.mention}",
        ephemeral=False
    )


@tree.command(name="removesetup", description="Disable confessions in this server")
@app_commands.checks.has_permissions(administrator=True)
async def removesetup(interaction: discord.Interaction):
    await interaction.response.defer()

    await settings_db.delete_one({"guild_id": interaction.guild.id})

    await interaction.followup.send(
        "Confession system disabled in this server.",
        ephemeral=False
    )


@tree.command(name="config", description="View server configuration")
async def config(interaction: discord.Interaction):
    await interaction.response.defer()

    config_data = await settings_db.find_one({"guild_id": interaction.guild.id})
    delay = await get_global_delay()

    if not config_data:
        await interaction.followup.send("Confession not set up in this server.")
        return

    channel = bot.get_channel(config_data["channel_id"])

    embed = discord.Embed(
        title="Server Confession Config",
        color=discord.Color.blurple()
    )
    embed.add_field(name="Channel", value=channel.mention if channel else "Not found")
    embed.add_field(name="Global Delay", value=f"{delay} seconds")

    await interaction.followup.send(embed=embed)


# ================= GLOBAL DELAY =================

@tree.command(name="setdelay", description="Set global confession delay (seconds)")
@app_commands.checks.has_permissions(administrator=True)
async def setdelay(interaction: discord.Interaction, seconds: int):
    await interaction.response.defer()

    if seconds < 1:
        await interaction.followup.send("Delay must be at least 1 second.")
        return

    await set_global_delay(seconds)

    await interaction.followup.send(
        f"Global delay set to {seconds} seconds.",
        ephemeral=False
    )


# ================= CONFESSION =================

@tree.command(name="confess", description="Send anonymous confession")
async def confess(interaction: discord.Interaction, message: str):
    await interaction.response.defer()

    user_id = interaction.user.id
    today = datetime.date.today().isoformat()
    now = datetime.datetime.now(datetime.UTC)

    # DAILY LIMIT
    record = await confessions_db.find_one({"user_id": user_id, "date": today})

    if record and record["count"] >= 3:
        await interaction.followup.send(
            "You have reached the daily limit of 3 confessions.",
            ephemeral=False
        )
        return

    # SWEAR FILTER
    banned_words = ["badword1", "badword2"]
    if any(word in message.lower() for word in banned_words):
        await interaction.followup.send(
            "Swear words are not allowed.",
            ephemeral=False
        )
        return

    # COOLDOWN (15 sec)
    cooldown_record = await cooldown_db.find_one({"user_id": user_id})

    if cooldown_record:
        last_used = cooldown_record["last_used"]
        if (now - last_used).total_seconds() < 15:
            await interaction.followup.send(
                "You are on cooldown. Please wait before confessing again.",
                ephemeral=False
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

    # Queue
    await confession_queue.put({"text": message})

    await interaction.followup.send(
        "Your confession has been added to the global queue.",
        ephemeral=False
    )


# ================= STATS =================

@tree.command(name="stats", description="View global confession stats")
async def stats(interaction: discord.Interaction):
    await interaction.response.defer()

    total_confessions = await confessions_db.count_documents({})
    total_servers = await settings_db.count_documents({})

    embed = discord.Embed(
        title="Global Stats",
        color=discord.Color.green()
    )
    embed.add_field(name="Total Confessions", value=str(total_confessions))
    embed.add_field(name="Servers Connected", value=str(total_servers))

    await interaction.followup.send(embed=embed)


bot.run(TOKEN)
