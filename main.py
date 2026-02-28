import os
import re
import random
import datetime
import discord
from discord import app_commands

from database import guilds, cooldowns
from cooldown import check_cooldown
from config import DEFAULT_EMBED_COLOR

TOKEN = os.getenv("TOKEN")

if not TOKEN:
    raise ValueError("TOKEN environment variable is not set.")

intents = discord.Intents.default()
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

# ------------------------
# CONFIG
# ------------------------

BANNED_WORDS = [
    "fuck",
    "shit",
    "bitch",
    "asshole",
    "nigger",
    "cunt"
]

ANONYMOUS_TAGS = [
    "Unknown Soul",
    "Hidden Mind",
    "Secret Whisper",
    "Anonymous Ghost",
    "Mystery Human",
    "Silent Stranger"
]

# ------------------------
# FILTER FUNCTIONS
# ------------------------

def normalize_text(text: str) -> str:
    """
    Removes spaces, symbols and repeated characters
    to prevent bypass like f.u.c.k or fuuuck
    """
    text = text.lower()
    text = re.sub(r'[^a-z]', '', text)  # remove symbols
    text = re.sub(r'(.)\1+', r'\1', text)  # remove repeated letters
    return text


def contains_banned_word(message: str) -> bool:
    normalized = normalize_text(message)
    for word in BANNED_WORDS:
        if word in normalized:
            return True
    return False


async def check_daily_limit(user_id: int) -> bool:
    """
    Returns True if user can send confession.
    Allows only 3 per day globally.
    """
    today = datetime.date.today().isoformat()

    record = await cooldowns.find_one({
        "user_id": user_id,
        "date": today
    })

    if not record:
        await cooldowns.insert_one({
            "user_id": user_id,
            "date": today,
            "count": 1
        })
        return True

    if record["count"] >= 3:
        return False

    await cooldowns.update_one(
        {"_id": record["_id"]},
        {"$inc": {"count": 1}}
    )

    return True


# ------------------------
# EVENTS
# ------------------------

@bot.event
async def on_ready():
    await tree.sync()
    print(f"Logged in as {bot.user}")


# ------------------------
# SETUP COMMAND
# ------------------------

@tree.command(name="setup", description="Setup confession system")
@app_commands.checks.has_permissions(administrator=True)
async def setup(
    interaction: discord.Interaction,
    confession_channel: discord.TextChannel,
    log_channel: discord.TextChannel,
    cooldown: int
):
    await guilds.update_one(
        {"guild_id": interaction.guild.id},
        {
            "$set": {
                "guild_id": interaction.guild.id,
                "confession_channel": confession_channel.id,
                "log_channel": log_channel.id,
                "cooldown": cooldown,
                "embed_color": DEFAULT_EMBED_COLOR
            }
        },
        upsert=True
    )

    await interaction.response.send_message(
        "Confession system configured successfully.",
        ephemeral=True
    )


# ------------------------
# CONFESS COMMAND (GLOBAL)
# ------------------------

@tree.command(name="confess", description="Send an anonymous confession")
async def confess(interaction: discord.Interaction, message: str):

    # Swear filter
    if contains_banned_word(message):
        await interaction.response.send_message(
            "Your confession contains inappropriate language.",
            ephemeral=True
        )
        return

    # Daily limit
    allowed_today = await check_daily_limit(interaction.user.id)
    if not allowed_today:
        await interaction.response.send_message(
            "You can only send 3 confessions per day.",
            ephemeral=True
        )
        return

    # Global cooldown (optional)
    allowed, remaining = await check_cooldown(
        interaction.user.id,
        0,  # global
        30
    )

    if not allowed:
        await interaction.response.send_message(
            f"You are on cooldown. Try again in {remaining} seconds.",
            ephemeral=True
        )
        return

    embed = discord.Embed(
        description=message,
        color=DEFAULT_EMBED_COLOR
    )

    random_tag = random.choice(ANONYMOUS_TAGS)
    embed.set_author(name=f"Confession from {random_tag}")

    # Send to ALL configured servers
    all_guilds = guilds.find()

    async for config in all_guilds:
        channel = bot.get_channel(config["confession_channel"])
        if channel:
            try:
                await channel.send(embed=embed)
            except:
                pass

    await interaction.response.send_message(
        "Your confession has been sent globally.",
        ephemeral=True
    )


bot.run(TOKEN)
