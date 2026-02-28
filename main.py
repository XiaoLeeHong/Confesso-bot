import os
import discord
from discord import app_commands

from database import guilds
from cooldown import check_cooldown
from config import DEFAULT_EMBED_COLOR


TOKEN = os.getenv("TOKEN")

if not TOKEN:
    raise ValueError("TOKEN environment variable is not set.")


intents = discord.Intents.default()
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)


@bot.event
async def on_ready():
    await tree.sync()
    print(f"Logged in as {bot.user}")


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


@tree.command(name="confess", description="Send an anonymous confession")
async def confess(interaction: discord.Interaction, message: str):
    config = await guilds.find_one({"guild_id": interaction.guild.id})

    if not config:
        await interaction.response.send_message(
            "Confession system is not configured. Use /setup first.",
            ephemeral=True
        )
        return

    allowed, remaining = await check_cooldown(
        interaction.user.id,
        interaction.guild.id,
        config.get("cooldown", 30)
    )

    if not allowed:
        await interaction.response.send_message(
            f"You are on cooldown. Try again in {remaining} seconds.",
            ephemeral=True
        )
        return

    confession_channel = bot.get_channel(config["confession_channel"])
    log_channel = bot.get_channel(config["log_channel"])

    embed = discord.Embed(
        description=message,
        color=config.get("embed_color", DEFAULT_EMBED_COLOR)
    )

    await confession_channel.send(embed=embed)

    if log_channel:
        await log_channel.send(
            f"Confession by {interaction.user} ({interaction.user.id})"
        )

    await interaction.response.send_message(
        "Your confession has been sent anonymously.",
        ephemeral=True
    )


bot.run(TOKEN)
