import discord
from discord.ext import commands, tasks
from discord import app_commands
import os
import threading
import json
import aiohttp
import datetime
from zoneinfo import ZoneInfo
import motor.motor_asyncio
from flask import Flask

# ---- Config ----
TOKEN = os.getenv("DISCORD_TOKEN")  # set this as an environment variable, don't paste your token here
MONGODB_URI = os.getenv("MONGODB_URI")  # e.g. mongodb+srv://user:pass@cluster.mongodb.net
STATUS_TEXT = "Olga Family: Season 4"  # change this to whatever you want

# Discord channel to post server member join/leave messages in
WELCOME_CHANNEL_ID = 1513932845922385920  # change this if you want a different channel

# Default greet/leave messages, used until someone sets a custom one with
# /setgreetmsg or /setleavemsg. Use {mention} (or {member}, same thing) to
# actually ping the person, or {name} for their display name with no ping.
DEFAULT_GREET_MSG = "welcome to the server {mention}, glad you're here"
DEFAULT_LEAVE_MSG = "{name} left the server"
DEFAULT_GREET_COLOR = discord.Color.green()
DEFAULT_LEAVE_COLOR = discord.Color.red()

# Color used for all of the bot's own regular replies (permission denials,
# confirmations, errors, mod command results, etc). Change this one value
# to recolor every "system" embed the bot sends at once.
SYSTEM_EMBED_COLOR = discord.Color.from_str("#f30d25")

# Only these Discord user IDs can use -send, /setgreetmsg, /setleavemsg -
# these commands can post as your bot or change server-wide messages, so
# keep this locked down to just you (and anyone else you trust).
# Right-click your name in Discord (with Developer Mode on) -> Copy User ID
ADMIN_IDS = [925226542571855943]  # replace with your actual Discord user ID

# Discord role ID allowed to use restricted commands (/startcountinground,
# /stopcountinground). Right-click the role in Server Settings -> Roles
# (with Developer Mode on) -> Copy Role ID.
MOD_ROLE_ID = 1515690428974891089  # replace with your actual moderator role ID

# Channel where Wordle game results (win/fail) and server-streak updates
# get posted.
WORDLE_ANNOUNCE_CHANNEL_ID = 1517175386021040138

# Set this to your server's ID for instant slash-command syncing during
# testing (guild syncs are instant; global syncs can take up to an hour
# to show up everywhere). Leave as None to sync globally instead.
DEV_GUILD_ID = 1469696264407879814  # e.g. 123456789012345678

# ---- MongoDB setup ----
# Used to persist per-guild greet/leave messages, counting rounds, and
# Wordle sessions/stats across restarts/redeploys.
mongo_client = motor.motor_asyncio.AsyncIOMotorClient(MONGODB_URI) if MONGODB_URI else None
db = mongo_client["olgabot"] if mongo_client else None
settings_collection = db["greet_leave_settings"] if db is not None else None
counting_collection = db["counting_channels"] if db is not None else None
wordle_sessions_collection = db["wordle_sessions"] if db is not None else None
wordle_stats_collection = db["wordle_stats"] if db is not None else None
wordle_daily_collection = db["wordle_daily_results"] if db is not None else None
wordle_streak_collection = db["wordle_server_streak"] if db is not None else None
wordle_meta_collection = db["wordle_meta"] if db is not None else None

# In-memory cache of per-guild messages/colors, loaded from MongoDB on
# startup and kept in sync whenever /setgreetmsg or /setleavemsg is used.
# Structure: { guild_id: {"greet": "...", "greet_color": int, "leave": "...", "leave_color": int} }
guild_messages = {}

# In-memory cache of active counting-game channels, loaded from MongoDB on
# startup and kept in sync on every count. Keyed by channel ID (globally
# unique), so multiple channels/guilds can each run their own round.
# Structure: { channel_id: {"guild_id": int, "count": int, "last_user_id": int|None, "double_count_allowed": bool} }
counting_state = {}

# In-memory cache of active (in-progress) Wordle games, loaded from MongoDB
# on startup so a game someone's mid-way through survives a restart. Keyed
# by user ID (one active game per person across all servers).
# Structure: { user_id: {"guild_id": int, "date": "YYYY-MM-DD", "word": str, "guesses": [{"word": str, "scores": [str,...]}]} }
active_wordle_sessions = {}

# Cache of today's Wordle answer, refetched once per day (see
# WORDLE_RESET_TIMEZONE below) so we're not hitting NYT's API on every guess.
# Structure: {"date": "YYYY-MM-DD" | None, "word": str | None}
wordle_word_cache = {"date": None, "word": None}

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

    # Wipe any leftover GLOBAL command registrations on Discord's side first.
    # Since DEV_GUILD_ID is set below, this bot only ever syncs to that one
    # guild - so any command that was ever registered globally in the past
    # (e.g. during early testing) has no other code path to get cleared, and
    # just sits there forever, showing up as a duplicate/stale entry in
    # Discord's command picker. This uses a raw API call (bulk-overwriting
    # Discord's global list with an empty one) instead of
    # bot.tree.clear_commands(), because clear_commands() would also wipe
    # our LOCAL command definitions below (they're registered without a
    # guild, i.e. as "global" in the tree) - which would leave nothing for
    # copy_global_to() to copy into the guild afterward.
    try:
        await bot.http.bulk_upsert_global_commands(bot.application_id, [])
        print("Cleared any stale global slash command(s)")
    except Exception as e:
        print(f"Failed to clear global slash commands: {e}")

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

    if not wordle_streak_loop.is_running():
        wordle_streak_loop.start()


# ---- Greet/leave message settings ----

async def load_guild_messages():
    """Load all per-guild greet/leave messages and colors from MongoDB on startup."""
    global guild_messages
    if settings_collection is None:
        print("[settings] MONGODB_URI not set, skipping load (using defaults/in-memory only)")
        return
    try:
        async for doc in settings_collection.find({}):
            guild_messages[doc["_id"]] = {
                "greet": doc.get("greet", DEFAULT_GREET_MSG),
                "greet_color": doc.get("greet_color", DEFAULT_GREET_COLOR.value),
                "leave": doc.get("leave", DEFAULT_LEAVE_MSG),
                "leave_color": doc.get("leave_color", DEFAULT_LEAVE_COLOR.value),
            }
        print(f"[settings] Loaded custom messages for {len(guild_messages)} guild(s)")
    except Exception as e:
        print(f"[settings] Failed to load settings from MongoDB: {type(e).__name__}: {e}")


async def save_guild_field(guild_id: int, field: str, value):
    """Persist a single field (greet, greet_color, leave, or leave_color) for a guild
    to MongoDB and update the in-memory cache."""
    guild_messages.setdefault(guild_id, {
        "greet": DEFAULT_GREET_MSG,
        "greet_color": DEFAULT_GREET_COLOR.value,
        "leave": DEFAULT_LEAVE_MSG,
        "leave_color": DEFAULT_LEAVE_COLOR.value,
    })
    guild_messages[guild_id][field] = value

    if settings_collection is None:
        return
    try:
        await settings_collection.update_one(
            {"_id": guild_id},
            {"$set": {field: value}},
            upsert=True,
        )
    except Exception as e:
        print(f"[settings] Failed to save {field} for guild {guild_id}: {type(e).__name__}: {e}")


def get_greet_message(guild_id: int) -> str:
    return guild_messages.get(guild_id, {}).get("greet", DEFAULT_GREET_MSG)


def get_leave_message(guild_id: int) -> str:
    return guild_messages.get(guild_id, {}).get("leave", DEFAULT_LEAVE_MSG)


def get_greet_color(guild_id: int) -> discord.Color:
    return discord.Color(guild_messages.get(guild_id, {}).get("greet_color", DEFAULT_GREET_COLOR.value))


def get_leave_color(guild_id: int) -> discord.Color:
    return discord.Color(guild_messages.get(guild_id, {}).get("leave_color", DEFAULT_LEAVE_COLOR.value))


# Named colors accepted by /setgreetmsg and /setleavemsg, in addition to hex
# codes like #57F287. Add more here if you want other named options.
NAMED_COLORS = {
    "red": discord.Color.red(),
    "green": discord.Color.green(),
    "blue": discord.Color.blue(),
    "blurple": discord.Color.blurple(),
    "greyple": discord.Color.greyple(),
    "gold": discord.Color.gold(),
    "orange": discord.Color.orange(),
    "purple": discord.Color.purple(),
    "magenta": discord.Color.magenta(),
    "teal": discord.Color.teal(),
    "dark_red": discord.Color.dark_red(),
    "dark_green": discord.Color.dark_green(),
    "dark_blue": discord.Color.dark_blue(),
    "dark_purple": discord.Color.dark_purple(),
    "yellow": discord.Color.yellow(),
    "black": discord.Color.from_str("#000000"),
    "white": discord.Color.from_str("#FFFFFF"),
}


def parse_color(color_str: str):
    """Parse a hex code (e.g. '#57F287' or '57F287') or a name from
    NAMED_COLORS into a discord.Color. Returns None if it can't be parsed."""
    color_str = color_str.strip()
    named = NAMED_COLORS.get(color_str.lower())
    if named is not None:
        return named
    hex_str = color_str.lstrip("#")
    try:
        return discord.Color(int(hex_str, 16))
    except ValueError:
        return None


def format_member_message(template: str, member: discord.Member) -> str:
    # {mention} and {member} both insert an actual ping (e.g. <@123456789>);
    # {name} inserts their display name with no ping.
    return template.format(mention=member.mention, member=member.mention, name=member.display_name)


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def system_embed(description: str) -> discord.Embed:
    """Build a consistently-colored embed for the bot's own replies
    (denials, confirmations, errors). Change SYSTEM_EMBED_COLOR above to
    recolor all of these at once."""
    return discord.Embed(description=description, color=SYSTEM_EMBED_COLOR)


@bot.tree.command(name="setgreetmsg", description="Set the message (and optionally color) posted when someone joins")
@app_commands.describe(
    message="Use {mention} to ping them, or {name} for their display name (no ping)",
    color="Hex code like #57F287, or a name like green, red, blue, gold, purple, etc. (optional)",
)
async def slash_setgreetmsg(interaction: discord.Interaction, message: str, color: str = None):
    if not is_admin(interaction.user.id):
        await interaction.response.send_message(embed=system_embed("You're not allowed to use this command."), ephemeral=True)
        return
    if interaction.guild is None:
        await interaction.response.send_message(embed=system_embed("This command can only be used in a server."), ephemeral=True)
        return

    await save_guild_field(interaction.guild.id, "greet", message)

    parsed_color = None
    color_note = ""
    if color:
        parsed_color = parse_color(color)
        if parsed_color is None:
            color_note = f"\n\n⚠️ Couldn't parse color `{color}` - message was saved, but the color wasn't changed."
        else:
            await save_guild_field(interaction.guild.id, "greet_color", parsed_color.value)

    preview_text = format_member_message(message, interaction.user) if isinstance(interaction.user, discord.Member) else message
    preview_color = parsed_color if parsed_color is not None else get_greet_color(interaction.guild.id)
    preview = discord.Embed(description=preview_text, color=preview_color)
    preview.set_footer(text="Greet message updated" + color_note)
    await interaction.response.send_message(embed=preview)


@bot.tree.command(name="setleavemsg", description="Set the message (and optionally color) posted when someone leaves")
@app_commands.describe(
    message="Use {mention} to ping them, or {name} for their display name (no ping)",
    color="Hex code like #ED4245, or a name like red, green, blue, gold, purple, etc. (optional)",
)
async def slash_setleavemsg(interaction: discord.Interaction, message: str, color: str = None):
    if not is_admin(interaction.user.id):
        await interaction.response.send_message(embed=system_embed("You're not allowed to use this command."), ephemeral=True)
        return
    if interaction.guild is None:
        await interaction.response.send_message(embed=system_embed("This command can only be used in a server."), ephemeral=True)
        return

    await save_guild_field(interaction.guild.id, "leave", message)

    parsed_color = None
    color_note = ""
    if color:
        parsed_color = parse_color(color)
        if parsed_color is None:
            color_note = f"\n\n⚠️ Couldn't parse color `{color}` - message was saved, but the color wasn't changed."
        else:
            await save_guild_field(interaction.guild.id, "leave_color", parsed_color.value)

    preview_text = format_member_message(message, interaction.user) if isinstance(interaction.user, discord.Member) else message
    preview_color = parsed_color if parsed_color is not None else get_leave_color(interaction.guild.id)
    preview = discord.Embed(description=preview_text, color=preview_color)
    preview.set_footer(text="Leave message updated" + color_note)
    await interaction.response.send_message(embed=preview)


# ---- Server join/leave messages ----
@bot.event
async def on_member_join(member):
    channel = bot.get_channel(WELCOME_CHANNEL_ID)
    if channel:
        template = get_greet_message(member.guild.id)
        embed = discord.Embed(
            description=format_member_message(template, member),
            color=get_greet_color(member.guild.id),
        )
        await channel.send(embed=embed)
    else:
        print(f"[welcome] Could not find channel with ID {WELCOME_CHANNEL_ID}")


@bot.event
async def on_member_remove(member):
    channel = bot.get_channel(WELCOME_CHANNEL_ID)
    if channel:
        template = get_leave_message(member.guild.id)
        embed = discord.Embed(
            description=format_member_message(template, member),
            color=get_leave_color(member.guild.id),
        )
        await channel.send(embed=embed)
    else:
        print(f"[welcome] Could not find channel with ID {WELCOME_CHANNEL_ID}")


# ---- Permission helpers ----
# Shared by the restricted commands below (/startcountinground,
# /stopcountinground). Restricted to members holding the MOD_ROLE_ID role,
# configured above.

def is_moderator(user) -> bool:
    if not isinstance(user, discord.Member):
        return False
    return any(role.id == MOD_ROLE_ID for role in user.roles)


async def moderator_check(interaction: discord.Interaction) -> bool:
    """Shared guard for restricted commands. Sends a denial reply and
    returns False if the command shouldn't proceed."""
    if interaction.guild is None:
        await interaction.response.send_message(embed=system_embed("This command can only be used in a server."), ephemeral=True)
        return False
    if not is_moderator(interaction.user):
        await interaction.response.send_message(embed=system_embed("You're not allowed to use this command."), ephemeral=True)
        return False
    return True


# ---- Counting game ----
# /startcountinground turns a channel into a counting game: people count up
# 1, 2, 3... one message at a time. Say the wrong number, say something
# that isn't a number, or (unless double counting is allowed) count twice
# in a row, and the round resets to 1.

COUNTING_RUIN_COLOR = discord.Color(15928613)


async def load_counting_state():
    """Load all active counting channels from MongoDB on startup, so a
    round's progress survives restarts/redeploys."""
    global counting_state
    if counting_collection is None:
        print("[counting] MONGODB_URI not set, skipping load (using in-memory only)")
        return
    try:
        async for doc in counting_collection.find({}):
            counting_state[doc["_id"]] = {
                "guild_id": doc["guild_id"],
                "count": doc.get("count", 0),
                "last_user_id": doc.get("last_user_id"),
                "double_count_allowed": doc.get("double_count_allowed", False),
            }
        print(f"[counting] Loaded {len(counting_state)} active counting channel(s)")
    except Exception as e:
        print(f"[counting] Failed to load state from MongoDB: {type(e).__name__}: {e}")


async def save_counting_state(channel_id: int):
    """Persist a single counting channel's current state to MongoDB."""
    state = counting_state.get(channel_id)
    if state is None or counting_collection is None:
        return
    try:
        await counting_collection.update_one(
            {"_id": channel_id},
            {"$set": {
                "guild_id": state["guild_id"],
                "count": state["count"],
                "last_user_id": state["last_user_id"],
                "double_count_allowed": state["double_count_allowed"],
            }},
            upsert=True,
        )
    except Exception as e:
        print(f"[counting] Failed to save state for channel {channel_id}: {type(e).__name__}: {e}")


async def ruin_counting(message: discord.Message, state: dict, attempted_number: int, reason: str):
    """Reset a counting round to 1 and post the ruin embed."""
    embed = discord.Embed(
        description=(
            f"Pfftth, stupid Olga {message.author.mention} ruined the counting at {attempted_number} for {reason}. "
            f"Start from 1 again\n\n"
            f"-Grabs belt-\n-Whips nonstop-\n-Disowns this stupid Olga-\n-Sends to SizzleBurger camp-"
        ),
        color=COUNTING_RUIN_COLOR,
    )
    state["count"] = 0
    state["last_user_id"] = None
    await save_counting_state(message.channel.id)
    await message.channel.send(embed=embed)


async def handle_counting_message(message: discord.Message):
    state = counting_state.get(message.channel.id)
    if state is None:
        return

    content = message.content.strip()
    expected = state["count"] + 1

    try:
        number = int(content)
    except ValueError:
        await ruin_counting(message, state, expected, "typing something that wasn't a number")
        return

    if not state["double_count_allowed"] and state["last_user_id"] is not None and state["last_user_id"] == message.author.id:
        await ruin_counting(message, state, expected, "counting twice in a row")
        return

    if number != expected:
        await ruin_counting(message, state, expected, f"saying {number} instead of {expected}")
        return

    state["count"] = number
    state["last_user_id"] = message.author.id
    await save_counting_state(message.channel.id)
    try:
        await message.add_reaction("☑️")
    except discord.HTTPException:
        pass


@bot.event
async def on_message(message: discord.Message):
    # Always let prefix commands (-ping, -send) keep working - overriding
    # on_message replaces discord.py's default handling of them.
    if message.author.bot:
        return

    if message.guild is None:
        if message.author.id in active_wordle_sessions:
            await handle_wordle_guess(message)
        await bot.process_commands(message)
        return

    if bot.user in message.mentions:
        await message.channel.send("what the hell do you want bitch")
        await bot.process_commands(message)
        return

    if (
        message.guild is not None
        and message.channel.id in counting_state
        and not message.content.startswith(bot.command_prefix)
    ):
        await handle_counting_message(message)

    await bot.process_commands(message)


@bot.tree.command(name="startcountinground", description="Start (or restart) a counting game in a channel")
@app_commands.describe(
    channel="Channel where counting will happen",
    double_count_allowed="Allow the same person to count twice in a row (default: no)",
    start_at="The last correct number already counted (e.g. carrying over from another bot). Default: 0, so counting begins at 1",
)
async def slash_startcountinground(interaction: discord.Interaction, channel: discord.TextChannel, double_count_allowed: bool = False, start_at: app_commands.Range[int, 0, None] = 0):
    if not await moderator_check(interaction):
        return

    counting_state[channel.id] = {
        "guild_id": interaction.guild.id,
        "count": start_at,
        "last_user_id": None,
        "double_count_allowed": double_count_allowed,
    }
    await save_counting_state(channel.id)

    await interaction.response.send_message(embed=system_embed(
        f"🔢 Counting round started in {channel.mention}. Next number: **{start_at + 1}**.\n"
        f"Double counting: {'allowed' if double_count_allowed else 'not allowed'}."
    ))


@bot.tree.command(name="stopcountinground", description="Stop the active counting game in a channel")
@app_commands.describe(channel="Channel to stop counting in")
async def slash_stopcountinground(interaction: discord.Interaction, channel: discord.TextChannel):
    if not await moderator_check(interaction):
        return

    state = counting_state.pop(channel.id, None)
    if state is None:
        await interaction.response.send_message(embed=system_embed(f"There's no active counting round in {channel.mention}."), ephemeral=True)
        return

    if counting_collection is not None:
        try:
            await counting_collection.delete_one({"_id": channel.id})
        except Exception as e:
            print(f"[counting] Failed to delete state for channel {channel.id}: {type(e).__name__}: {e}")

    await interaction.response.send_message(embed=system_embed(
        f"🛑 Counting round stopped in {channel.mention}. Final count reached: **{state['count']}**."
    ))


# ---- Wordle ----
# /wordle DMs the person that day's real NYT Wordle puzzle to play, one
# guess per DM. /wordlestats and /wordleleaderboard read back the streaks
# and stats tracked in MongoDB.

WORDLE_MAX_GUESSES = 6
WORDLE_TILE = {"green": "🟩", "yellow": "🟨", "gray": "⬛"}

# The Wordle "day" resets when the clock hits midnight in THIS timezone -
# not UTC. UTC was the original approach, but it rolls over mid-afternoon/
# evening for US timezones, which caused the puzzle to appear to reset
# hours before someone's actual local midnight (and then NOT reset again
# at their real midnight, since the UTC date hadn't changed yet) - hence
# the "you already played today" bug. Change this to whatever timezone
# should govern the reset for your server. Full list of valid names:
# https://en.wikipedia.org/wiki/List_of_tz_database_time_zones
WORDLE_RESET_TIMEZONE = ZoneInfo("America/Chicago")


def today_wordle_date_str() -> str:
    return datetime.datetime.now(WORDLE_RESET_TIMEZONE).date().isoformat()


async def get_wordle_of_day():
    """Fetch (and cache for the rest of the day in WORDLE_RESET_TIMEZONE)
    today's real answer from the NYT Wordle API. Returns (word, date_str),
    or (None, date_str) if the fetch fails."""
    global wordle_word_cache
    date_str = today_wordle_date_str()
    if wordle_word_cache["date"] == date_str and wordle_word_cache["word"]:
        return wordle_word_cache["word"], date_str

    url = f"https://www.nytimes.com/svc/wordle/v2/{date_str}.json"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    print(f"[wordle] Fetching puzzle failed: HTTP {resp.status}")
                    return None, date_str
                data = await resp.json()
    except Exception as e:
        print(f"[wordle] Failed to fetch puzzle: {type(e).__name__}: {e}")
        return None, date_str

    word = data.get("solution")
    if not word:
        return None, date_str
    word = word.lower()
    wordle_word_cache = {"date": date_str, "word": word}
    return word, date_str


def score_guess(guess: str, target: str):
    """Score a guess against the target the way real Wordle does, including
    correct handling of repeated letters. Returns a list of 5 strings, each
    'green', 'yellow', or 'gray'."""
    result = [None] * 5
    remaining = list(target)

    for i in range(5):
        if guess[i] == target[i]:
            result[i] = "green"
            remaining[i] = None

    for i in range(5):
        if result[i] is not None:
            continue
        if guess[i] in remaining:
            result[i] = "yellow"
            remaining[remaining.index(guess[i])] = None
        else:
            result[i] = "gray"

    return result


def render_wordle_board(guesses) -> str:
    rows = []
    for g in guesses:
        tiles = "".join(WORDLE_TILE[s] for s in g["scores"])
        letters = " ".join(c.upper() for c in g["word"])
        rows.append(f"{tiles}\n{letters}")
    return "\n\n".join(rows)


async def load_wordle_sessions():
    """Load any in-progress Wordle games from MongoDB on startup, so a game
    someone's mid-way through survives a restart/redeploy."""
    global active_wordle_sessions
    if wordle_sessions_collection is None:
        print("[wordle] MONGODB_URI not set, skipping session load (using in-memory only)")
        return
    try:
        async for doc in wordle_sessions_collection.find({}):
            active_wordle_sessions[doc["_id"]] = {
                "guild_id": doc["guild_id"],
                "date": doc["date"],
                "word": doc["word"],
                "guesses": doc.get("guesses", []),
            }
        print(f"[wordle] Loaded {len(active_wordle_sessions)} active session(s)")
    except Exception as e:
        print(f"[wordle] Failed to load sessions from MongoDB: {type(e).__name__}: {e}")


async def save_wordle_session(user_id: int):
    session = active_wordle_sessions.get(user_id)
    if session is None or wordle_sessions_collection is None:
        return
    try:
        await wordle_sessions_collection.update_one({"_id": user_id}, {"$set": session}, upsert=True)
    except Exception as e:
        print(f"[wordle] Failed to save session for user {user_id}: {type(e).__name__}: {e}")


async def delete_wordle_session(user_id: int):
    if wordle_sessions_collection is not None:
        try:
            await wordle_sessions_collection.delete_one({"_id": user_id})
        except Exception as e:
            print(f"[wordle] Failed to delete session for user {user_id}: {type(e).__name__}: {e}")


def wordle_stats_key(guild_id: int, user_id: int) -> str:
    return f"{guild_id}:{user_id}"


async def get_wordle_stats(guild_id: int, user_id: int):
    if wordle_stats_collection is None:
        return None
    return await wordle_stats_collection.find_one({"_id": wordle_stats_key(guild_id, user_id)})


async def update_wordle_stats(guild_id: int, user_id: int, won: bool, guesses_used: int, date_str: str):
    """Record the result of a finished game: games played, wins, current/max
    streak, and the guess-count distribution (for wins only, like real
    Wordle's stats screen)."""
    if wordle_stats_collection is None:
        return

    key = wordle_stats_key(guild_id, user_id)
    doc = await wordle_stats_collection.find_one({"_id": key}) or {}

    games_played = doc.get("games_played", 0) + 1
    wins = doc.get("wins", 0) + (1 if won else 0)
    current_streak = doc.get("current_streak", 0) + 1 if won else 0
    max_streak = max(doc.get("max_streak", 0), current_streak)

    distribution = doc.get("distribution", {})
    if won:
        distribution[str(guesses_used)] = distribution.get(str(guesses_used), 0) + 1

    try:
        await wordle_stats_collection.update_one(
            {"_id": key},
            {"$set": {
                "guild_id": guild_id,
                "user_id": user_id,
                "games_played": games_played,
                "wins": wins,
                "current_streak": current_streak,
                "max_streak": max_streak,
                "distribution": distribution,
                "last_played_date": date_str,
            }},
            upsert=True,
        )
    except Exception as e:
        print(f"[wordle] Failed to update stats for user {user_id}: {type(e).__name__}: {e}")


def wordle_daily_key(guild_id: int, date_str: str) -> str:
    return f"{guild_id}:{date_str}"


async def record_daily_win(guild_id: int, date_str: str):
    """Mark that at least one person won in this guild on this date - the
    server streak loop checks this to decide whether the streak continues."""
    if wordle_daily_collection is None:
        return
    try:
        await wordle_daily_collection.update_one(
            {"_id": wordle_daily_key(guild_id, date_str)},
            {"$set": {"guild_id": guild_id, "date": date_str}, "$inc": {"win_count": 1}},
            upsert=True,
        )
    except Exception as e:
        print(f"[wordle] Failed to record daily win for guild {guild_id}: {type(e).__name__}: {e}")


async def announce_wordle_result(user: discord.abc.User, session: dict, won: bool, guesses_used: int):
    """Post a win/fail announcement to WORDLE_ANNOUNCE_CHANNEL_ID."""
    channel = bot.get_channel(WORDLE_ANNOUNCE_CHANNEL_ID)
    if channel is None:
        return

    if won:
        embed = discord.Embed(
            description=f"🎉 {user.mention} solved today's Wordle in **{guesses_used}/{WORDLE_MAX_GUESSES}**!",
            color=discord.Color.green(),
        )
    else:
        embed = discord.Embed(
            description=f"💀 {user.mention} failed today's Wordle. The word was **{session['word'].upper()}**.",
            color=discord.Color.red(),
        )

    try:
        await channel.send(embed=embed)
    except discord.HTTPException as e:
        print(f"[wordle] Failed to post result announcement: {type(e).__name__}: {e}")


async def finish_wordle_game(user_id: int, session: dict, won: bool):
    if won:
        await record_daily_win(session["guild_id"], session["date"])
    await update_wordle_stats(session["guild_id"], user_id, won, len(session["guesses"]), session["date"])
    active_wordle_sessions.pop(user_id, None)
    await delete_wordle_session(user_id)


async def handle_wordle_guess(message: discord.Message):
    session = active_wordle_sessions.get(message.author.id)
    if session is None:
        return

    guess = message.content.strip().lower()
    if len(guess) != 5 or not guess.isalpha():
        await message.channel.send(embed=system_embed("-slaps in the back of the head- guesses need to be a single 5 letter word stupid hoe. do i need to explain it again??"))
        return

    scores = score_guess(guess, session["word"])
    session["guesses"].append({"word": guess, "scores": scores})
    await save_wordle_session(message.author.id)

    board = render_wordle_board(session["guesses"])
    won = guess == session["word"]
    out_of_guesses = len(session["guesses"]) >= WORDLE_MAX_GUESSES

    if won:
        embed = discord.Embed(
            description=f"{board}\n\n🎉 **good job hoe, u got it in {len(session['guesses'])}/{WORDLE_MAX_GUESSES}!**",
            color=discord.Color.green(),
        )
        await message.channel.send(embed=embed)
        await announce_wordle_result(message.author, session, won=True, guesses_used=len(session["guesses"]))
        await finish_wordle_game(message.author.id, session, won=True)
        return

    if out_of_guesses:
        embed = discord.Embed(
            description=f"{board}\n\n💀 stupid fatass bitch, you're a disappointment to this family. The word was **{session['word'].upper()}**.",
            color=discord.Color.red(),
        )
        await message.channel.send(embed=embed)
        await announce_wordle_result(message.author, session, won=False, guesses_used=len(session["guesses"]))
        await finish_wordle_game(message.author.id, session, won=False)
        return

    guesses_left = WORDLE_MAX_GUESSES - len(session["guesses"])
    embed = discord.Embed(description=f"{board}\n\nGuesses left: {guesses_left}", color=SYSTEM_EMBED_COLOR)
    await message.channel.send(embed=embed)


@bot.tree.command(name="wordle", description="play the wordle today, its pretty obivous")
async def slash_wordle(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message(embed=system_embed("just run the command stupid hoe?? ill dm you it"), ephemeral=True)
        return

    if interaction.user.id in active_wordle_sessions:
        await interaction.response.send_message(embed=system_embed("you already have a game going on in our dms, are you that stupid?"), ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    word, date_str = await get_wordle_of_day()
    if word is None:
        await interaction.followup.send(embed=system_embed("i couldnt get the wordle for today oops"), ephemeral=True)
        return

    stats_doc = await get_wordle_stats(interaction.guild.id, interaction.user.id)
    if stats_doc and stats_doc.get("last_played_date") == date_str:
        await interaction.followup.send(embed=system_embed("you already played the worlde today, dumbass. go get checked for alzheimers"), ephemeral=True)
        return

    active_wordle_sessions[interaction.user.id] = {
        "guild_id": interaction.guild.id,
        "date": date_str,
        "word": word,
        "guesses": [],
    }
    await save_wordle_session(interaction.user.id)

    try:
        await interaction.user.send(embed=discord.Embed(
            description=(
                "🟩 **wordle time** reply here with your guesses, one 5 letter word per message. is it that hard to understand hoe?\n"
                f"you have {WORDLE_MAX_GUESSES} guesses. dont fuck it up"
            ),
            color=SYSTEM_EMBED_COLOR,
        ))
    except discord.HTTPException:
        del active_wordle_sessions[interaction.user.id]
        await delete_wordle_session(interaction.user.id)
        await interaction.followup.send(embed=system_embed("your dms are off bitch, why though? youre not a celebrity, go turn those dms on hoe"), ephemeral=True)
        return

    await interaction.followup.send(embed=system_embed("go run to your dms for the game, bitch. that's the only running we'll ever see from you"), ephemeral=True)


@bot.tree.command(name="wordlestats", description="check your (or someone else's if your nosy) Wordle stats")
@app_commands.describe(member="Whose stats to view (default: yourself)")
async def slash_wordlestats(interaction: discord.Interaction, member: discord.Member = None):
    if interaction.guild is None:
        await interaction.response.send_message(embed=system_embed("This command can only be used in a server."), ephemeral=True)
        return

    target = member or interaction.user
    doc = await get_wordle_stats(interaction.guild.id, target.id)
    if not doc:
        await interaction.response.send_message(embed=system_embed(f"{target.display_name} hasn't played Wordle here yet, wow, what a dumb bitch."))
        return

    games_played = doc.get("games_played", 0)
    wins = doc.get("wins", 0)
    win_pct = round(wins / games_played * 100) if games_played else 0
    distribution = doc.get("distribution", {})
    max_count = max((int(v) for v in distribution.values()), default=0)

    dist_lines = []
    for i in range(1, WORDLE_MAX_GUESSES + 1):
        count = int(distribution.get(str(i), 0))
        bar_len = round((count / max_count) * 10) if max_count else 0
        bar = "🟩" * bar_len if bar_len else "▫️"
        dist_lines.append(f"`{i}` {bar} {count}")

    embed = discord.Embed(title=f"Wordle stats - {target.display_name}", color=SYSTEM_EMBED_COLOR)
    embed.set_thumbnail(url=target.display_avatar.url)
    embed.add_field(name="Games played", value=str(games_played))
    embed.add_field(name="Win %", value=f"{win_pct}%")
    embed.add_field(name="Current streak", value=f"🔥 {doc.get('current_streak', 0)}")
    embed.add_field(name="Max streak", value=str(doc.get("max_streak", 0)))
    embed.add_field(name="Guess distribution", value="\n".join(dist_lines), inline=False)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="wordleleaderboard", description="see the top Wordle players")
async def slash_wordleleaderboard(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message(embed=system_embed("This command can only be used in a server."), ephemeral=True)
        return

    if wordle_stats_collection is None:
        await interaction.response.send_message(embed=system_embed("The leaderboard isn't available (no database configured)."), ephemeral=True)
        return

    cursor = wordle_stats_collection.find({"guild_id": interaction.guild.id}).sort(
        [("current_streak", -1), ("wins", -1)]
    ).limit(10)
    docs = await cursor.to_list(length=10)

    if not docs:
        await interaction.response.send_message(embed=system_embed("No one has played Wordle here yet."))
        return

    lines = []
    for i, doc in enumerate(docs, start=1):
        lines.append(f"**{i}.** <@{doc['user_id']}> - 🔥 {doc.get('current_streak', 0)} streak, {doc.get('wins', 0)} wins")

    embed = discord.Embed(title="🏆 Wordle Leaderboard", description="\n".join(lines), color=SYSTEM_EMBED_COLOR)
    await interaction.response.send_message(embed=embed)


# ---- Wordle new-puzzle announcement ----
# Posts to WORDLE_ANNOUNCE_CHANNEL_ID as soon as a new day's puzzle becomes
# available (checked on the same periodic loop as the streak evaluation,
# below). Also pre-warms the word cache so the first /wordle of the day
# doesn't have to wait on the NYT fetch.

async def check_new_wordle_puzzle():
    if wordle_meta_collection is None:
        return

    today_str = today_wordle_date_str()
    meta_doc = await wordle_meta_collection.find_one({"_id": "puzzle_announce"})
    last_seen = meta_doc.get("last_seen_date") if meta_doc else None

    if last_seen == today_str:
        return  # already handled today, nothing to do

    if last_seen is None:
        # First time this check has EVER run (e.g. right after deploying
        # this feature, or the bot's very first startup) - just record
        # today as the baseline without announcing. Otherwise every fresh
        # deploy would immediately blast "today's Wordle is ready" even if
        # that day's puzzle has already been out for hours.
        try:
            await wordle_meta_collection.update_one(
                {"_id": "puzzle_announce"},
                {"$set": {"last_seen_date": today_str}},
                upsert=True,
            )
        except Exception as e:
            print(f"[wordle] Failed to save puzzle-check baseline: {type(e).__name__}: {e}")
        return

    # last_seen holds a real previous date that doesn't match today - a
    # genuine day rollover happened since our last check (or the bot was
    # down across one and is catching up now), so announce it.
    word, date_str = await get_wordle_of_day()
    if word is None:
        return  # fetch failed - try again next tick, don't update last_seen yet

    channel = bot.get_channel(WORDLE_ANNOUNCE_CHANNEL_ID)
    if channel is not None:
        try:
            await channel.send(embed=discord.Embed(
                description="🚬 -takes a smoke- -coughs until i pass out- YO YO YO HOESSSSS!!!!!!!! TODAYS WORDLE IS READY, GO PLAY NOW. RUN /wordle",
                color=SYSTEM_EMBED_COLOR,
            ))
        except discord.HTTPException as e:
            print(f"[wordle] Failed to post new puzzle announcement: {type(e).__name__}: {e}")

    try:
        await wordle_meta_collection.update_one(
            {"_id": "puzzle_announce"},
            {"$set": {"last_seen_date": date_str}},
            upsert=True,
        )
    except Exception as e:
        print(f"[wordle] Failed to save puzzle announcement marker: {type(e).__name__}: {e}")


# ---- Wordle server streak ----
# The server streak goes up by 1 for each day AT LEAST ONE person in the
# server wins that day's Wordle. If a day passes with nobody playing, or
# nobody who played got it right, the streak resets to 0. Checked once per
# day (see wordle_streak_loop below) rather than the instant someone wins,
# since we can't know a day was a total loss until it's actually over.

def wordle_date_str_add(date_str: str, days: int) -> str:
    return (datetime.date.fromisoformat(date_str) + datetime.timedelta(days=days)).isoformat()


async def evaluate_guild_wordle_streak(guild: discord.Guild):
    if wordle_streak_collection is None:
        return

    today_str = today_wordle_date_str()
    yesterday_str = wordle_date_str_add(today_str, -1)

    streak_doc = await wordle_streak_collection.find_one({"_id": guild.id})
    if streak_doc and streak_doc.get("last_evaluated_date") == yesterday_str:
        return  # already evaluated for this day rollover

    old_streak = streak_doc.get("streak", 0) if streak_doc else 0

    daily_doc = None
    if wordle_daily_collection is not None:
        daily_doc = await wordle_daily_collection.find_one({"_id": wordle_daily_key(guild.id, yesterday_str)})
    had_win = bool(daily_doc and daily_doc.get("win_count", 0) > 0)

    new_streak = old_streak + 1 if had_win else 0

    try:
        await wordle_streak_collection.update_one(
            {"_id": guild.id},
            {"$set": {"guild_id": guild.id, "streak": new_streak, "last_evaluated_date": yesterday_str}},
            upsert=True,
        )
    except Exception as e:
        print(f"[wordle] Failed to update server streak for guild {guild.id}: {type(e).__name__}: {e}")

    if new_streak == old_streak:
        return  # nothing changed (e.g. brand new server, no one's played yet) - stay quiet

    channel = bot.get_channel(WORDLE_ANNOUNCE_CHANNEL_ID)
    if channel is None:
        return

    try:
        if had_win:
            await channel.send(embed=discord.Embed(
                description=f"🔥 wow, one of you actually turned on that fermented cobweb of a brain. the server Wordle streak is now **{new_streak}**",
                color=discord.Color.green(),
            ))
        else:
            await channel.send(embed=discord.Embed(
                description=f"💔 Stupid hoes, nobody solved yesterday's Wordle, the server streak of **{old_streak}** has been lost",
                color=discord.Color.red(),
            ))
    except discord.HTTPException as e:
        print(f"[wordle] Failed to post streak update: {type(e).__name__}: {e}")


@tasks.loop(time=datetime.time(hour=0, minute=1, tzinfo=WORDLE_RESET_TIMEZONE))
async def wordle_streak_loop():
    await check_new_wordle_puzzle()
    for guild in bot.guilds:
        await evaluate_guild_wordle_streak(guild)


@wordle_streak_loop.before_loop
async def before_wordle_streak_loop():
    await bot.wait_until_ready()


# ---- Prefix commands (e.g. -ping) ----

# Example command so you know it's alive - try "-ping" in your server
@bot.command()
async def ping(ctx):
    await ctx.send("cunt")


# ---- Slash commands (e.g. /ping) ----
# These are what show up in Discord's "/" menu. They require the bot to be
# invited with the "applications.commands" scope (not just "bot"), and for
# bot.tree.sync() to have run at least once (handled in on_ready above).

@bot.tree.command(name="ping", description="Check if the bot is alive")
async def slash_ping(interaction: discord.Interaction):
    await interaction.response.send_message("cunt")


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
        await ctx.send(embed=system_embed("You're not allowed to use this command."))
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
        await ctx.send(embed=system_embed(f"That's not valid JSON: {e}"))
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
        await ctx.send(embed=system_embed(f"Discord rejected it ({status}): {result.get('message', result)}"))
    else:
        await ctx.send(embed=system_embed(f"Sent to {channel.mention}"))


keep_alive()

@bot.event
async def setup_hook():
    await load_guild_messages()
    await load_counting_state()
    await load_wordle_sessions()

bot.run(TOKEN)