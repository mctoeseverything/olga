import discord
from discord.ext import commands
import os
import threading
import requests
import json
import aiohttp
import motor.motor_asyncio
from flask import Flask
from discord.ext import tasks

# ---- Group rank presence tracker config ----
GROUP_ID = 32860910  # your Roblox group ID
TRACKED_ROLE_IDS = [100231905]  # role IDs to watch
TARGET_PLACE_ID = 17333697975  # the specific game's place ID to watch for
NOTIFY_CHANNEL_ID = 1513932318119825548  # Discord channel ID to post notifications in

# ---- Config ----
TOKEN = os.getenv("DISCORD_TOKEN")  # set this as an environment variable, don't paste your token here
GROQ_API_KEY = os.getenv("GROQ_API_KEY")  # get this free from console.groq.com
ROBLOX_COOKIE = os.getenv("ROBLOX_COOKIE")  # .ROBLOSECURITY cookie value, see setup notes
MONGODB_URI = os.getenv("MONGODB_URI")  # e.g. mongodb+srv://user:pass@cluster.mongodb.net
STATUS_TEXT = "Olga Family: Season 4"  # change this to whatever you want

# Discord channel to post server member join/leave messages in
WELCOME_CHANNEL_ID = 1513932845922385920  # change this if you want a different channel

# Only these Discord user IDs can use -send - this command can post as your
# bot to ANY channel it has access to, so keep this locked down to just you.
# Right-click your name in Discord (with Developer Mode on) -> Copy User ID
ADMIN_IDS = [925226542571855943]  # replace with your actual Discord user ID

# Set this to your server's ID for instant slash-command syncing during
# testing (guild syncs are instant; global syncs can take up to an hour
# to show up everywhere). Leave as None to sync globally instead.
DEV_GUILD_ID = None  # e.g. 123456789012345678

# ---- MongoDB setup ----
# Used to persist tracker state (tracked user IDs + who's currently in-game)
# across restarts/redeploys, so we don't lose state or send spurious
# "just joined" notifications when the bot comes back up.
mongo_client = motor.motor_asyncio.AsyncIOMotorClient(MONGODB_URI) if MONGODB_URI else None
db = mongo_client["olgabot"] if mongo_client else None
state_collection = db["tracker_state"] if db is not None else None

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


# ---- Server join/leave messages ----
@bot.event
async def on_member_join(member):
    channel = bot.get_channel(WELCOME_CHANNEL_ID)
    if channel:
        await channel.send(f"welcome to the server {member.mention}, glad you're here")
    else:
        print(f"[welcome] Could not find channel with ID {WELCOME_CHANNEL_ID}")


@bot.event
async def on_member_remove(member):
    channel = bot.get_channel(WELCOME_CHANNEL_ID)
    if channel:
        await channel.send(f"{member.display_name} left the server")
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


# ---- Group rank presence tracker ----
# Watches a list of user IDs (people holding specific ranks in your Roblox
# group) and notifies Discord when one of them joins a specific game -
# without naming who it was.

tracked_user_ids = set()
last_in_game = set()  # user IDs we last saw in the target game


async def load_state():
    """Load tracked_user_ids and last_in_game from MongoDB on startup.
    Keeps state across restarts/redeploys so we don't lose the tracked
    list or send spurious join notifications when the bot comes back up."""
    global tracked_user_ids, last_in_game
    if state_collection is None:
        print("[tracker] MONGODB_URI not set, skipping state load (using in-memory only)")
        return
    try:
        doc = await state_collection.find_one({"_id": "tracker"})
        if doc:
            tracked_user_ids = set(doc.get("tracked_user_ids", []))
            last_in_game = set(doc.get("last_in_game", []))
            print(f"[tracker] Loaded state from MongoDB: {len(tracked_user_ids)} tracked, {len(last_in_game)} in-game")
        else:
            print("[tracker] No existing state found in MongoDB")
    except Exception as e:
        print(f"[tracker] Failed to load state from MongoDB: {type(e).__name__}: {e}")


async def save_state():
    """Persist tracked_user_ids and last_in_game to MongoDB."""
    if state_collection is None:
        return
    try:
        await state_collection.update_one(
            {"_id": "tracker"},
            {"$set": {
                "tracked_user_ids": list(tracked_user_ids),
                "last_in_game": list(last_in_game),
            }},
            upsert=True,
        )
    except Exception as e:
        print(f"[tracker] Failed to save state to MongoDB: {type(e).__name__}: {e}")


async def refresh_tracked_users():
    """Pull user IDs for the tracked role(s) from Roblox's public group API."""
    global tracked_user_ids
    ids = set()
    async with aiohttp.ClientSession() as session:
        for role_id in TRACKED_ROLE_IDS:
            cursor = ""
            while True:
                url = f"https://groups.roblox.com/v1/groups/{GROUP_ID}/roles/{role_id}/users?limit=100&cursor={cursor}"
                async with session.get(url) as resp:
                    if resp.status != 200:
                        print(f"[tracker] Failed to fetch role {role_id}: {resp.status}")
                        break
                    data = await resp.json()
                    for user in data.get("data", []):
                        ids.add(user["userId"])
                    cursor = data.get("nextPageCursor")
                    if not cursor:
                        break
    tracked_user_ids = ids
    print(f"[tracker] Refreshed tracked list: {len(tracked_user_ids)} user(s) found for role(s) {TRACKED_ROLE_IDS}")
    await save_state()


async def get_csrf_token(session):
    """Roblox requires an X-CSRF-TOKEN header for authenticated POST requests.
    It's handed back in the response headers of a deliberately-failed request."""
    async with session.post(
        "https://auth.roblox.com/v2/logout",
        headers={"Cookie": f".ROBLOSECURITY={ROBLOX_COOKIE}"},
    ) as resp:
        return resp.headers.get("x-csrf-token")


async def check_presence():
    """Check presence for all tracked users and notify on new joins/leaves for the target game."""
    global last_in_game
    if not tracked_user_ids or TARGET_PLACE_ID is None or NOTIFY_CHANNEL_ID is None:
        print(f"[tracker] Skipping check - tracked_user_ids={len(tracked_user_ids)}, TARGET_PLACE_ID={TARGET_PLACE_ID}, NOTIFY_CHANNEL_ID={NOTIFY_CHANNEL_ID}")
        return

    headers = {}
    if ROBLOX_COOKIE:
        headers["Cookie"] = f".ROBLOSECURITY={ROBLOX_COOKIE}"

    async with aiohttp.ClientSession() as session:
        if ROBLOX_COOKIE:
            csrf = await get_csrf_token(session)
            if csrf:
                headers["X-CSRF-TOKEN"] = csrf
            else:
                print("[tracker] Could not get CSRF token - cookie may be invalid/expired")

        async with session.post(
            "https://presence.roblox.com/v1/presence/users",
            json={"userIds": list(tracked_user_ids)},
            headers=headers,
        ) as resp:
            if resp.status != 200:
                print(f"[tracker] Presence check failed: {resp.status}")
                return
            data = await resp.json()

    currently_in_game = set()
    for entry in data.get("userPresences", []):
        # userPresenceType 2 = InGame. Check both placeId and rootPlaceId,
        # since some experiences route joins through a different place ID
        # than the one shown in the URL (e.g. multi-place games).
        if entry.get("userPresenceType") == 2 and (
            entry.get("placeId") == TARGET_PLACE_ID or entry.get("rootPlaceId") == TARGET_PLACE_ID
        ):
            currently_in_game.add(entry["userId"])

    print(f"[tracker] Checked {len(tracked_user_ids)} user(s), {len(currently_in_game)} currently in target game")

    new_joins = currently_in_game - last_in_game
    new_leaves = last_in_game - currently_in_game
    last_in_game = currently_in_game
    await save_state()

    if new_joins or new_leaves:
        channel = bot.get_channel(NOTIFY_CHANNEL_ID)
        if not channel:
            print(f"[tracker] Could not find channel with ID {NOTIFY_CHANNEL_ID}")
            return

        for _ in new_joins:
            try:
                msg = await channel.send("Someone on the watchlist just joined the game.")
                print(f"[tracker] Join notification sent, message ID: {msg.id}")
            except Exception as e:
                print(f"[tracker] Failed to send join notification: {type(e).__name__}: {e}")

        for _ in new_leaves:
            try:
                msg = await channel.send("Someone on the watchlist just left the game.")
                print(f"[tracker] Leave notification sent, message ID: {msg.id}")
            except Exception as e:
                print(f"[tracker] Failed to send leave notification: {type(e).__name__}: {e}")


@tasks.loop(seconds=60)
async def presence_loop():
    await check_presence()


@tasks.loop(hours=72)
async def refresh_loop():
    await refresh_tracked_users()


@presence_loop.before_loop
@refresh_loop.before_loop
async def before_loops():
    await bot.wait_until_ready()


keep_alive()

@bot.event
async def setup_hook():
    await load_state()
    refresh_loop.start()
    presence_loop.start()

bot.run(TOKEN)