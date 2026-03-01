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

DEFAULT_DELAY = 2
DAILY_LIMIT = 3
COOLDOWN_SECONDS = 15
GUILD_MIN_INTERVAL = 10

guild_last_sent = {}

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
    normalized = re.sub(r'(.)\1{4,}', r'\1', lower)

    if re.search(r"(discord.gg/|discord.com/invite/)", normalized):
        return True, "Invites are not allowed."

    if re.search(r"(https?://|www.|.com\b|.net\b|.org\b)", normalized):
        return True, "Links are not allowed."

    if any(re.search(rf"\b{re.escape(word)}\b", normalized) for word in BAD_WORDS):
        return True, "Inappropriate language detected."

    if len(message) > 1000:
        return True, "Message too long."

    if len(message) > 15:
        caps_ratio = sum(c.isupper() for c in message) / len(message)
        if caps_ratio > 0.7:
            return True, "Too many caps."

    return False, None

# ================= VOTE VIEW =================

class VoteView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.likes = 0
        self.dislikes = 0

    @discord.ui.button(label="👍 0", style=discord.ButtonStyle.green)
    async def like(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.likes += 1
        button.label = f"👍 {self.likes}"
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="👎 0", style=discord.ButtonStyle.red)
    async def dislike(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.dislikes += 1
        button.label = f"👎 {self.dislikes}"
        await interaction.response.edit_message(view=self)

# ================= WORKER =================

async def confession_worker():
    await bot.wait_until_ready()
    print("Worker started.")

    while not bot.is_closed():
        try:
            data = await confession_queue.get()
            confession_id = data["id"]
            message = data["text"]

            embed = discord.Embed(
                title=f"🌍 Global Confession #{confession_id}",
                description=message,
                color=discord.Color.purple(),
                timestamp=datetime.datetime.now(datetime.timezone.utc)
            )

            async for guild_data in settings_db.find({}):
                guild_id = guild_data.get("guild_id")
                channel_id = guild_data.get("channel_id")

                guild = bot.get_guild(guild_id)
                if not guild:
                    continue

                now = datetime.datetime.now(datetime.timezone.utc)
                last = guild_last_sent.get(guild_id)

                if last and (now - last).total_seconds() < GUILD_MIN_INTERVAL:
                    continue

                channel = guild.get_channel(channel_id)
                if not channel:
                    continue

                perms = channel.permissions_for(guild.me)
                if not perms.send_messages or not perms.embed_links:
                    continue

                try:
                    await channel.send(embed=embed, view=VoteView())
                    guild_last_sent[guild_id] = now
                except Exception as e:
                    print(f"Send error: {e}")

            await asyncio.sleep(DEFAULT_DELAY)

        except Exception as e:
            print(f"Worker error: {e}")
            await asyncio.sleep(5)

# ================= EVENTS =================

@bot.event
async def on_ready():
    global worker_started

    await confessions_db.create_index([("user_id", 1), ("date", 1)])
    await settings_db.create_index("guild_id", unique=True)
    await cooldown_db.create_index("user_id", unique=True)

    if not worker_started:
        bot.loop.create_task(confession_worker())
        worker_started = True

    await tree.sync()
    print(f"Logged in as {bot.user}")

# ================= SETUP =================

@tree.command(name="setup")
@app_commands.checks.has_permissions(administrator=True)
async def setup(interaction: discord.Interaction, channel: discord.TextChannel):
    perms = channel.permissions_for(interaction.guild.me)

    if not perms.send_messages or not perms.embed_links:
        await interaction.response.send_message(
            "I need send_messages and embed_links in that channel.",
            ephemeral=True
        )
        return

    await settings_db.update_one(
        {"guild_id": interaction.guild.id},
        {"$set": {"guild_id": interaction.guild.id, "channel_id": channel.id}},
        upsert=True
    )

    await interaction.response.send_message(
        "Server joined global confession network.",
        ephemeral=True
    )

# ================= CONFESS =================

@tree.command(name="confess")
async def confess(interaction: discord.Interaction, message: str):
    await interaction.response.defer(ephemeral=True)

    user_id = interaction.user.id
    guild_id = interaction.guild.id
    now = datetime.datetime.now(datetime.timezone.utc)
    today = now.date().isoformat()

    if await global_bans_db.find_one({"user_id": user_id}):
        await interaction.followup.send("You are globally banned.", ephemeral=True)
        return

    cooldown = await cooldown_db.find_one({"user_id": user_id})
    if cooldown:
        diff = (now - cooldown["last_used"]).total_seconds()
        if diff < COOLDOWN_SECONDS:
            await interaction.followup.send(
                f"Cooldown active. Try again in {int(COOLDOWN_SECONDS - diff)}s.",
                ephemeral=True
            )
            return

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

    message = discord.utils.escape_mentions(message)

    confession_id = await generate_confession_id()

    await confessions_db.insert_one({
        "confession_id": confession_id,
        "user_id": user_id,
        "guild_origin": guild_id,
        "message": message,
        "date": today
    })

    await cooldown_db.update_one(
        {"user_id": user_id},
        {"$set": {"last_used": now}},
        upsert=True
    )

    await confession_queue.put({"id": confession_id, "text": message})

    await interaction.followup.send(
        "Your confession was sent globally.",
        ephemeral=True
    )

# ================= FUN COMMANDS =================

@tree.command(name="truth")
async def truth(interaction: discord.Interaction):
    questions = [
        "What is your biggest secret?",
        "Who was your first crush?",
        "What is something you regret?",
        "What lie do you tell often?",
        "What is your hidden fear?"
    ]
    await interaction.response.send_message(
        f"🕵️ Truth: {questions[datetime.datetime.utcnow().second % len(questions)]}"
    )

@tree.command(name="dare")
async def dare(interaction: discord.Interaction):
    dares = [
        "Change your nickname for 1 hour.",
        "Send a random emoji in general.",
        "Compliment someone anonymously.",
        "Reveal your screen time.",
        "Post your last screenshot."
    ]
    await interaction.response.send_message(
        f"🔥 Dare: {dares[datetime.datetime.utcnow().second % len(dares)]}"
    )

@tree.command(name="globalstats")
async def globalstats(interaction: discord.Interaction):
    total = await confessions_db.count_documents({})
    await interaction.response.send_message(
        f"🌍 Total global confessions: {total}"
    )

bot.run(TOKEN)
