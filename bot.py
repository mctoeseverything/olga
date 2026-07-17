import discord
from discord.ext import commands
from discord import app_commands
import os
import threading
import requests
import json
import aiohttp
import datetime
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

# Discord role ID allowed to use moderation commands (/kick, /ban, /warn,
# /ground, /modlogs). Right-click the role in Server Settings -> Roles
# (with Developer Mode on) -> Copy Role ID.
MOD_ROLE_ID = 1515690428974891089  # replace with your actual moderator role ID

# Set this to your server's ID for instant slash-command syncing during
# testing (guild syncs are instant; global syncs can take up to an hour
# to show up everywhere). Leave as None to sync globally instead.
DEV_GUILD_ID = 1469696264407879814  # e.g. 123456789012345678

# ---- MongoDB setup ----
# Used to persist per-guild greet/leave messages and moderation history
# across restarts/redeploys.
mongo_client = motor.motor_asyncio.AsyncIOMotorClient(MONGODB_URI) if MONGODB_URI else None
db = mongo_client["olgabot"] if mongo_client else None
settings_collection = db["greet_leave_settings"] if db is not None else None
mod_actions_collection = db["mod_actions"] if db is not None else None
counting_collection = db["counting_channels"] if db is not None else None

# In-memory cache of per-guild messages/colors, loaded from MongoDB on
# startup and kept in sync whenever /setgreetmsg or /setleavemsg is used.
# Structure: { guild_id: {"greet": "...", "greet_color": int, "leave": "...", "leave_color": int} }
guild_messages = {}

# In-memory cache of active counting-game channels, loaded from MongoDB on
# startup and kept in sync on every count. Keyed by channel ID (globally
# unique), so multiple channels/guilds can each run their own round.
# Structure: { channel_id: {"guild_id": int, "count": int, "last_user_id": int|None, "double_count_allowed": bool} }
counting_state = {}

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


# ---- Moderation ----
# /kick, /ban, /warn, /ground (timeout), and /modlogs (history lookup).
# Restricted to members holding the MOD_ROLE_ID role, configured above.

MOD_ACTION_LABELS = {
    "kick": "Kicked",
    "ban": "Banned",
    "warn": "Warned",
    "ground": "Grounded",
}

# Discord's hard cap on timeouts is 28 days.
MAX_GROUND_MINUTES = 40320


def is_moderator(user) -> bool:
    if not isinstance(user, discord.Member):
        return False
    return any(role.id == MOD_ROLE_ID for role in user.roles)


async def log_mod_action(guild_id: int, target_id: int, action_type: str, reason: str, moderator_id: int, duration_minutes: int = None):
    """Record a moderation action so it shows up in /modlogs."""
    if mod_actions_collection is None:
        print("[mod] MONGODB_URI not set, moderation action was not logged")
        return
    doc = {
        "guild_id": guild_id,
        "target_id": target_id,
        "type": action_type,
        "reason": reason,
        "moderator_id": moderator_id,
        "timestamp": datetime.datetime.now(datetime.timezone.utc),
    }
    if duration_minutes is not None:
        doc["duration_minutes"] = duration_minutes
    try:
        await mod_actions_collection.insert_one(doc)
    except Exception as e:
        print(f"[mod] Failed to log {action_type} for user {target_id}: {type(e).__name__}: {e}")


async def get_mod_history(guild_id: int, target_id: int, limit: int = 15):
    """Return a user's past moderation actions in this guild, most recent first."""
    if mod_actions_collection is None:
        return []
    try:
        cursor = mod_actions_collection.find(
            {"guild_id": guild_id, "target_id": target_id}
        ).sort("timestamp", -1).limit(limit)
        return await cursor.to_list(length=limit)
    except Exception as e:
        print(f"[mod] Failed to fetch history for user {target_id}: {type(e).__name__}: {e}")
        return []


async def moderator_check(interaction: discord.Interaction) -> bool:
    """Shared guard for all moderation commands. Sends a denial reply and
    returns False if the command shouldn't proceed."""
    if interaction.guild is None:
        await interaction.response.send_message(embed=system_embed("This command can only be used in a server."), ephemeral=True)
        return False
    if not is_moderator(interaction.user):
        await interaction.response.send_message(embed=system_embed("You're not allowed to use this command."), ephemeral=True)
        return False
    return True


async def send_punishment_dm(member: discord.Member, description: str, moderator: discord.Member):
    """DM the punished member with the action embed and a disabled gray
    button crediting the moderator who did it. Silently does nothing if
    their DMs are closed (or the bot no longer shares a server with them)."""
    dm_view = discord.ui.View()
    dm_view.add_item(discord.ui.Button(
        label=f"Sent with hate from: {moderator.display_name}",
        style=discord.ButtonStyle.gray,
        disabled=True,
    ))
    try:
        await member.send(embed=system_embed(description), view=dm_view)
    except discord.HTTPException:
        pass


@bot.tree.command(name="kick", description="Kick a member from the server")
@app_commands.describe(member="Who to kick", reason="Why they're being kicked")
async def slash_kick(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):
    if not await moderator_check(interaction):
        return

    # DM before kicking - once they're removed, the bot may no longer
    # share a server with them and the DM could fail to send.
    await send_punishment_dm(member, f"You were kicked from **{interaction.guild.name}**: {reason}", interaction.user)

    try:
        await member.kick(reason=f"{reason} (by {interaction.user})")
    except discord.Forbidden:
        await interaction.response.send_message(embed=system_embed("I don't have permission to kick that member."), ephemeral=True)
        return
    except discord.HTTPException as e:
        await interaction.response.send_message(embed=system_embed(f"Failed to kick: {e}"), ephemeral=True)
        return

    await log_mod_action(interaction.guild.id, member.id, "kick", reason, interaction.user.id)
    await interaction.response.send_message(embed=system_embed(f"👢 Kicked {member.mention} - {reason}"))


@bot.tree.command(name="ban", description="Ban a member from the server")
@app_commands.describe(member="Who to ban", reason="Why they're being banned", delete_message_days="Days of their message history to delete (0-7, default 0)")
async def slash_ban(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided", delete_message_days: app_commands.Range[int, 0, 7] = 0):
    if not await moderator_check(interaction):
        return

    # DM before banning - once they're removed, the bot may no longer
    # share a server with them and the DM could fail to send.
    await send_punishment_dm(member, f"You were banned from **{interaction.guild.name}**: {reason}", interaction.user)

    try:
        await member.ban(reason=f"{reason} (by {interaction.user})", delete_message_seconds=delete_message_days * 86400)
    except discord.Forbidden:
        await interaction.response.send_message(embed=system_embed("I don't have permission to ban that member."), ephemeral=True)
        return
    except discord.HTTPException as e:
        await interaction.response.send_message(embed=system_embed(f"Failed to ban: {e}"), ephemeral=True)
        return

    await log_mod_action(interaction.guild.id, member.id, "ban", reason, interaction.user.id)
    await interaction.response.send_message(embed=system_embed(f"🔨 Banned {member.mention} - {reason}"))


@bot.tree.command(name="warn", description="Log a warning against a member")
@app_commands.describe(member="Who to warn", reason="What they're being warned for")
async def slash_warn(interaction: discord.Interaction, member: discord.Member, reason: str):
    if not await moderator_check(interaction):
        return

    await log_mod_action(interaction.guild.id, member.id, "warn", reason, interaction.user.id)
    await interaction.response.send_message(embed=system_embed(f"⚠️ Warned {member.mention} - {reason}"))
    await send_punishment_dm(member, f"You were warned in **{interaction.guild.name}**: {reason}", interaction.user)


@bot.tree.command(name="ground", description="Timeout (ground) a member for a set number of minutes")
@app_commands.describe(member="Who to ground", duration_minutes="How long to ground them for, in minutes", reason="Why they're being grounded")
async def slash_ground(interaction: discord.Interaction, member: discord.Member, duration_minutes: app_commands.Range[int, 1, MAX_GROUND_MINUTES], reason: str = "No reason provided"):
    if not await moderator_check(interaction):
        return

    until = discord.utils.utcnow() + datetime.timedelta(minutes=duration_minutes)
    try:
        await member.timeout(until, reason=f"{reason} (by {interaction.user})")
    except discord.Forbidden:
        await interaction.response.send_message(embed=system_embed("I don't have permission to ground that member."), ephemeral=True)
        return
    except discord.HTTPException as e:
        await interaction.response.send_message(embed=system_embed(f"Failed to ground: {e}"), ephemeral=True)
        return

    await log_mod_action(interaction.guild.id, member.id, "ground", reason, interaction.user.id, duration_minutes=duration_minutes)
    await interaction.response.send_message(embed=system_embed(f"🧎 Grounded {member.mention} for {duration_minutes} minute(s) - {reason}"))


@bot.tree.command(name="modlogs", description="View a member's past moderation history")
@app_commands.describe(member="Whose history to look up")
async def slash_modlogs(interaction: discord.Interaction, member: discord.Member):
    if not await moderator_check(interaction):
        return

    history = await get_mod_history(interaction.guild.id, member.id)

    embed = discord.Embed(
        title=f"Moderation history - {member.display_name}",
        color=SYSTEM_EMBED_COLOR,
    )
    embed.set_thumbnail(url=member.display_avatar.url)

    if not history:
        embed.description = "No moderation actions on record."
    else:
        for entry in history:
            label = MOD_ACTION_LABELS.get(entry["type"], entry["type"].title())
            timestamp = entry["timestamp"]
            unix_ts = int(timestamp.replace(tzinfo=datetime.timezone.utc).timestamp()) if timestamp.tzinfo is None else int(timestamp.timestamp())
            field_name = f"{label} - <t:{unix_ts}:R>"
            field_value = f"Reason: {entry.get('reason', 'No reason provided')}\nBy: <@{entry['moderator_id']}>"
            if entry.get("duration_minutes") is not None:
                field_value += f"\nDuration: {entry['duration_minutes']} minute(s)"
            embed.add_field(name=field_name, value=field_value, inline=False)

    await interaction.response.send_message(embed=embed)


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
    # Always let prefix commands (-ping, -roast, -send) keep working -
    # overriding on_message replaces discord.py's default handling of them.
    if message.author.bot:
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

bot.run(TOKEN)