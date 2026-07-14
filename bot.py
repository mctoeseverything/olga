import discord
from discord.ext import commands
import os
import threading
import requests
import json
import aiohttp
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
STATUS_TEXT = "Olga Family: Season 3.5"  # change this to whatever you want

# Only these Discord user IDs can use -send - this command can post as your
# bot to ANY channel it has access to, so keep this locked down to just you.
# Right-click your name in Discord (with Developer Mode on) -> Copy User ID
ADMIN_IDS = [123456789012345678]  # replace with your actual Discord user ID

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

bot = commands.Bot(command_prefix="-", intents=intents)


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    await bot.change_presence(
        activity=discord.Activity(type=discord.ActivityType.watching, name=STATUS_TEXT),
        status=discord.Status.online
    )


# Example command so you know it's alive - try "!ping" in your server
@bot.command()
async def ping(ctx):
    await ctx.send("cunt")


# Roast command - usage: !roast @someone
@bot.command()
async def roast(ctx, member: discord.Member = None):
    member = member or ctx.author  # roast yourself if no one is tagged
    await ctx.typing()

    prompt = (
     f"Write a short, savage roast (1-2 sentences) for {member.display_name}. "
f"Be extremely rude, mean, and brutal. Be creative, not traditional. Use curse words. Roast their fat ass, ugly face, stupid personality, smell, laziness — go hard. "
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
        roast_text = response.json()["choices"][0]["message"]["content"].strip()
        await ctx.send(f"{member.mention} {roast_text}")
    except Exception as e:
        print(f"Roast command error: {e}")
        await ctx.send("no sry ask daddy jay for help")


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


async def check_presence():
    """Check presence for all tracked users and notify on new joins to the target game."""
    global last_in_game
    if not tracked_user_ids or TARGET_PLACE_ID is None or NOTIFY_CHANNEL_ID is None:
        print(f"[tracker] Skipping check - tracked_user_ids={len(tracked_user_ids)}, TARGET_PLACE_ID={TARGET_PLACE_ID}, NOTIFY_CHANNEL_ID={NOTIFY_CHANNEL_ID}")
        return

    async with aiohttp.ClientSession() as session:
        async with session.post(
            "https://presence.roblox.com/v1/presence/users",
            json={"userIds": list(tracked_user_ids)},
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
    last_in_game = currently_in_game

    if new_joins:
        print(f"[tracker] {len(new_joins)} new join(s) detected, sending notification")
        channel = bot.get_channel(NOTIFY_CHANNEL_ID)
        if channel:
            for _ in new_joins:
                await channel.send("Someone on the watchlist just joined the game.")
        else:
            print(f"[tracker] Could not find channel with ID {NOTIFY_CHANNEL_ID}")


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
    refresh_loop.start()
    presence_loop.start()

bot.run(TOKEN)