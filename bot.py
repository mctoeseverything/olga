import discord
from discord.ext import commands
from discord import app_commands
import os
import threading
import requests
import json
import aiohttp
import motor.motor_asyncio
from flask import Flask

# ---- Config ----
TOKEN = os.getenv("DISCORD_TOKEN")  # set this as an environment variable, don't paste your token here
GROQ_API_KEY = os.getenv("GROQ_API_KEY")  # get this free from console.groq.com
MONGODB_URI = os.getenv("MONGODB_URI")  # e.g. mongodb+srv://user:pass@cluster.mongodb.net
STATUS_TEXT = "Olga Family: Season 4"  # change this to whatever you want

# Discord channel to post server member join/leave messages in
WELCOME_CHANNEL_ID = 1513932845922385920  # change this if you want a different channel

# Default greet/leave messages, used until someone sets a custom one with
# /setgreetmsg or /setleavemsg. Use {mention} (or {member}, same thing) to
# actually ping the person, or {name} for their display name with no ping.
DEFAULT_GREET_MSG = "welcome to the server {mention}, glad you're here"
DEFAULT_LEAVE_MSG = "{name} left the server"

# Only these Discord user IDs can use -send, /setgreetmsg, /setleavemsg -
# these commands can post as your bot or change server-wide messages, so
# keep this locked down to just you (and anyone else you trust).
# Right-click your name in Discord (with Developer Mode on) -> Copy User ID
ADMIN_IDS = [925226542571855943]  # replace with your actual Discord user ID

# Set this to your server's ID for instant slash-command syncing during
# testing (guild syncs are instant; global syncs can take up to an hour
# to show up everywhere). Leave as None to sync globally instead.
DEV_GUILD_ID = 1469696264407879814  # e.g. 123456789012345678

# ---- MongoDB setup ----
# Used to persist per-guild greet/leave messages across restarts/redeploys.
mongo_client = motor.motor_asyncio.AsyncIOMotorClient(MONGODB_URI) if MONGODB_URI else None
db = mongo_client["olgabot"] if mongo_client else None
settings_collection = db["greet_leave_settings"] if db is not None else None

# In-memory cache of per-guild messages, loaded from MongoDB on startup and
# kept in sync whenever /setgreetmsg or /setleavemsg is used.
# Structure: { guild_id: {"greet": "...", "leave": "..."} }
guild_messages = {}

# ---- Keep-alive web server ----
# Render needs an open port to consider the service "alive", and a free
# uptime pinger (like UptimeRobot) needs a URL to hit every few minutes
# so Render doesn't spin the service down from inactivity.
app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is alive!"

def run_web():
    port = int(os.environ.get("PORT", 8080))  # Render sets PORT automatically
    app.run(host="0.0.0.0", port=port)

def keep_alive():
    t = threading.Thread(target=run_web)
    t.start()

intents = discord.Intents.default()
intents.message_content = True  # needed if you want commands to work later
intents.members = True  # required for on_member_join / on_member_remove to fire

bot = commands.Bot(command_prefix="-", intents=intents)


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    await bot.change_presence(
        activity=discord.Activity(type=discord.ActivityType.watching, name=STATUS_TEXT),
        status=discord.Status.online
    )

    # Sync slash (/) commands with Discord.
    # Guild-specific sync shows up instantly - good for testing.
    # Global sync (no guild) can take up to an hour to propagate everywhere.
    try:
        if DEV_GUILD_ID:
            guild = discord.Object(id=DEV_GUILD_ID)
            bot.tree.copy_global_to(guild=guild)
            synced = await bot.tree.sync(guild=guild)
            print(f"Synced {len(synced)} slash command(s) to guild {DEV_GUILD_ID}")
        else:
            synced = await bot.tree.sync()
            print(f"Synced {len(synced)} slash command(s) globally")
    except Exception as e:
        print(f"Slash command sync failed: {e}")


# ---- Greet/leave message settings ----

async def load_guild_messages():
    """Load all per-guild greet/leave messages from MongoDB on startup."""
    global guild_messages
    if settings_collection is None:
        print("[settings] MONGODB_URI not set, skipping load (using defaults/in-memory only)")
        return
    try:
        async for doc in settings_collection.find({}):
            guild_messages[doc["_id"]] = {
                "greet": doc.get("greet", DEFAULT_GREET_MSG),
                "leave": doc.get("leave", DEFAULT_LEAVE_MSG),
            }
        print(f"[settings] Loaded custom messages for {len(guild_messages)} guild(s)")
    except Exception as e:
        print(f"[settings] Failed to load settings from MongoDB: {type(e).__name__}: {e}")


async def save_guild_message(guild_id: int, key: str, message: str):
    """Persist a single guild's greet or leave message to MongoDB and update the cache."""
    guild_messages.setdefault(guild_id, {"greet": DEFAULT_GREET_MSG, "leave": DEFAULT_LEAVE_MSG})
    guild_messages[guild_id][key] = message

    if settings_collection is None:
        return
    try:
        await settings_collection.update_one(
            {"_id": guild_id},
            {"$set": {key: message}},
            upsert=True,
        )
    except Exception as e:
        print(f"[settings] Failed to save {key} message for guild {guild_id}: {type(e).__name__}: {e}")


def get_greet_message(guild_id: int) -> str:
    return guild_messages.get(guild_id, {}).get("greet", DEFAULT_GREET_MSG)


def get_leave_message(guild_id: int) -> str:
    return guild_messages.get(guild_id, {}).get("leave", DEFAULT_LEAVE_MSG)


def format_member_message(template: str, member: discord.Member) -> str:
    # {mention} and {member} both insert an actual ping (e.g. <@123456789>);
    # {name} inserts their display name with no ping.
    return template.format(mention=member.mention, member=member.mention, name=member.display_name)


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


@bot.tree.command(name="setgreetmsg", description="Set the message posted when someone joins the server")
@app_commands.describe(message="Use {mention} to ping them, or {name} for their display name (no ping)")
async def slash_setgreetmsg(interaction: discord.Interaction, message: str):
    if not is_admin(interaction.user.id):
        await interaction.response.send_message("You're not allowed to use this command.", ephemeral=True)
        return
    if interaction.guild is None:
        await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
        return

    await save_guild_message(interaction.guild.id, "greet", message)
    preview = format_member_message(message, interaction.user) if isinstance(interaction.user, discord.Member) else message
    await interaction.response.send_message(f"Greet message updated. Preview: {preview}")


@bot.tree.command(name="setleavemsg", description="Set the message posted when someone leaves the server")
@app_commands.describe(message="Use {mention} to ping them, or {name} for their display name (no ping)")
async def slash_setleavemsg(interaction: discord.Interaction, message: str):
    if not is_admin(interaction.user.id):
        await interaction.response.send_message("You're not allowed to use this command.", ephemeral=True)
        return
    if interaction.guild is None:
        await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
        return

    await save_guild_message(interaction.guild.id, "leave", message)
    preview = format_member_message(message, interaction.user) if isinstance(interaction.user, discord.Member) else message
    await interaction.response.send_message(f"Leave message updated. Preview: {preview}")


# ---- Server join/leave messages ----
@bot.event
async def on_member_join(member):
    channel = bot.get_channel(WELCOME_CHANNEL_ID)
    if channel:
        template = get_greet_message(member.guild.id)
        await channel.send(format_member_message(template, member))
    else:
        print(f"[welcome] Could not find channel with ID {WELCOME_CHANNEL_ID}")


@bot.event
async def on_member_remove(member):
    channel = bot.get_channel(WELCOME_CHANNEL_ID)
    if channel:
        template = get_leave_message(member.guild.id)
        await channel.send(format_member_message(template, member))
    else:
        print(f"[welcome] Could not find channel with ID {WELCOME_CHANNEL_ID}")


# ---- Prefix commands (e.g. -ping) ----

# Example command so you know it's alive - try "-ping" in your server
@bot.command()
async def ping(ctx):
    await ctx.send("cunt")


# Roast command - usage: -roast @someone
@bot.command()
async def roast(ctx, member: discord.Member = None):
    member = member or ctx.author  # roast yourself if no one is tagged
    await ctx.typing()
    roast_text = await generate_roast(member.display_name)
    await ctx.send(f"{member.mention} {roast_text}")


# ---- Slash commands (e.g. /ping) ----
# These are what show up in Discord's "/" menu. They require the bot to be
# invited with the "applications.commands" scope (not just "bot"), and for
# bot.tree.sync() to have run at least once (handled in on_ready above).

@bot.tree.command(name="ping", description="Check if the bot is alive")
async def slash_ping(interaction: discord.Interaction):
    await interaction.response.send_message("cunt")


@bot.tree.command(name="roast", description="Roast someone (or yourself)")
@discord.app_commands.describe(member="Who to roast (leave blank to roast yourself)")
async def slash_roast(interaction: discord.Interaction, member: discord.Member = None):
    member = member or interaction.user
    await interaction.response.defer()  # roast takes a sec (API call), so defer first
    roast_text = await generate_roast(member.display_name)
    await interaction.followup.send(f"{member.mention} {roast_text}")


async def generate_roast(display_name: str) -> str:
    """Shared roast-generation logic used by both the prefix and slash commands."""
    prompt = (
        f"Write a short, savage roast (1 sentence) for {display_name}. "
        f"Be extremely rude, mean, and brutal. Be creative, not traditional. Use curse words. "
        f"Roast their fatass, ugly face, stupid personality, smell, laziness — go hard. "
        f"Make it funny and vicious. "
        f"Absolutely no race, ethnicity, sexuality, or homophobic shit."
    )

    try:
        response = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": "llama-3.3-70b-versatile",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 150,
            },
            timeout=20,
        )
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"Roast command error: {e}")
        return "no sry ask daddy jay for help"


# ---- Generic message sender ----
# Sends any valid Discord message payload (plain text, embeds, image
# attachments, or Components V2) to a channel you specify - no need to
# write a new command every time you want to send something different.
#
# Usage: -send #channel  followed by a JSON payload in a code block.
# You can also attach images/files directly to the same Discord message -
# they get forwarded and can be referenced in your JSON via
# "attachment://filename.png" (e.g. inside an embed image or a Components V2
# Media Gallery item).
#
# Examples of the JSON part:
#
# Plain text:
#   {"content": "hello everyone"}
#
# Embed:
#   {"embeds": [{"title": "Announcement", "description": "Big news!", "color": 3066993}]}
#
# Embed with an attached image:
#   {"embeds": [{"title": "Look at this", "image": {"url": "attachment://photo.png"}}]}
#   (attach photo.png to the Discord message itself)
#
# Components V2 (requires flags: 32768, and content/embeds are typically
# omitted since V2 replaces them with a components tree):
#   {"flags": 32768, "components": [{"type": 17, "components": [{"type": 10, "content": "# Big header\nSome text"}]}]}

DISCORD_API = "https://discord.com/api/v10"


async def send_raw_message(channel_id, payload: dict, files=None):
    headers = {"Authorization": f"Bot {TOKEN}"}
    url = f"{DISCORD_API}/channels/{channel_id}/messages"
    async with aiohttp.ClientSession() as session:
        if files:
            form = aiohttp.FormData()
            form.add_field("payload_json", json.dumps(payload), content_type="application/json")
            for i, f in enumerate(files):
                form.add_field(
                    f"files[{i}]",
                    f["data"],
                    filename=f["filename"],
                    content_type=f.get("content_type") or "application/octet-stream",
                )
            async with session.post(url, headers=headers, data=form) as resp:
                return await resp.json(), resp.status
        else:
            headers["Content-Type"] = "application/json"
            async with session.post(url, headers=headers, json=payload) as resp:
                return await resp.json(), resp.status


@bot.command()
async def send(ctx, channel: discord.TextChannel, *, payload: str):
    if ctx.author.id not in ADMIN_IDS:
        await ctx.send("You're not allowed to use this command.")
        return

    payload = payload.strip()
    if payload.startswith("```"):
        parts = payload.split("```")
        payload = parts[1]
        if payload.lower().startswith("json"):
            payload = payload[4:]
    payload = payload.strip()

    try:
        data = json.loads(payload)
    except json.JSONDecodeError as e:
        await ctx.send(f"That's not valid JSON: {e}")
        return

    files = []
    for att in ctx.message.attachments:
        files.append({
            "filename": att.filename,
            "data": await att.read(),
            "content_type": att.content_type,
        })

    result, status = await send_raw_message(channel.id, data, files=files or None)

    if status >= 300:
        await ctx.send(f"Discord rejected it ({status}): {result.get('message', result)}")
    else:
        await ctx.send(f"Sent to {channel.mention}")


keep_alive()

@bot.event
async def setup_hook():
    await load_guild_messages()

bot.run(TOKEN)