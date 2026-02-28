import discord
from discord import app_commands
from discord.ext import commands
import asyncio
import motor.motor_asyncio
import os
import datetime

TOKEN = os.getenv("TOKEN")
MONGO_URL = os.getenv("MONGO_URL")

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

mongo = motor.motor_asyncio.AsyncIOMotorClient(MONGO_URL)
db = mongo["confession_bot"]
confessions_db = db["confessions"]
settings_db = db["settings"]
cooldown_db = db["cooldowns"]

confession_queue = asyncio.Queue()
global_delay = 5  # default delay in seconds


# ================= BACKGROUND WORKER =================

async def confession_worker():
    await bot.wait_until_ready()
    while not bot.is_closed():
        data = await confession_queue.get()
        confession_text = data["text"]

        settings = settings_db.find({})
        async for guild_data in settings:
            guild = bot.get_guild(guild_data["guild_id"])
            if guild:
                channel = guild.get_channel(guild_data["channel_id"])
                if channel:
                    embed = discord.Embed(
                        title="New Anonymous Confession",
                        description=confession_text,
                        color=discord.Color.purple()
                    )
                    await channel.send(embed=embed)

        await asyncio.sleep(global_delay)


@bot.event
async def on_ready():
    bot.loop.create_task(confession_worker())
    await tree.sync()
    print(f"Logged in as {bot.user}")


# ================= SETUP COMMAND =================

@tree.command(name="setup", description="Set confession channel")
@app_commands.checks.has_permissions(administrator=True)
async def setup(interaction: discord.Interaction, channel: discord.TextChannel):
    await interaction.response.defer()

    await settings_db.update_one(
        {"guild_id": interaction.guild.id},
        {"$set": {"channel_id": channel.id}},
        upsert=True
    )

    await interaction.followup.send(
        f"Confession channel set to {channel.mention}",
        ephemeral=False
    )


# ================= SET GLOBAL DELAY =================

@tree.command(name="setdelay", description="Set global confession delay in seconds")
@app_commands.checks.has_permissions(administrator=True)
async def setdelay(interaction: discord.Interaction, seconds: int):
    global global_delay
    await interaction.response.defer()

    global_delay = seconds

    await interaction.followup.send(
        f"Global confession delay set to {seconds} seconds.",
        ephemeral=False
    )


# ================= CONFESSION COMMAND =================

@tree.command(name="confess", description="Send anonymous confession")
async def confess(interaction: discord.Interaction, message: str):
    await interaction.response.defer()

    user_id = interaction.user.id
    today = datetime.date.today().isoformat()

    # DAILY LIMIT CHECK
    record = await confessions_db.find_one({"user_id": user_id, "date": today})

    if record and record["count"] >= 3:
        await interaction.followup.send(
            "You have reached the daily limit of 3 confessions.",
            ephemeral=False
        )
        return

    # SWEAR FILTER (basic example)
    banned_words = ["badword1", "badword2"]
    if any(word in message.lower() for word in banned_words):
        await interaction.followup.send(
            "Swear words are not allowed in confessions.",
            ephemeral=False
        )
        return

    # COOLDOWN CHECK (10 seconds example)
    cooldown_record = await cooldown_db.find_one({"user_id": user_id})
    now = datetime.datetime.utcnow()

    if cooldown_record:
        last_used = cooldown_record["last_used"]
        if (now - last_used).total_seconds() < 10:
            await interaction.followup.send(
                "You are on cooldown. Please wait a few seconds.",
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

    # Add to global queue
    await confession_queue.put({"text": message})

    await interaction.followup.send(
        "Your confession has been added to the global queue.",
        ephemeral=False
    )


bot.run(TOKEN)
