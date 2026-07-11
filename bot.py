import discord
from discord.ext import commands
import os
import threading
import requests
from flask import Flask

# ---- Config ----
TOKEN = os.getenv("DISCORD_TOKEN")  # set this as an environment variable, don't paste your token here
GROQ_API_KEY = os.getenv("GROQ_API_KEY")  # get this free from console.groq.com
STATUS_TEXT = "Olga Family: Season 3.5"  # change this to whatever you want

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

bot = commands.Bot(command_prefix="!", intents=intents)


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
    await ctx.send("Cunt")


# Roast command - usage: !roast @someone
@bot.command()
async def roast(ctx, member: discord.Member = None):
    member = member or ctx.author  # roast yourself if no one is tagged
    await ctx.typing()

    prompt = (
        f"Write a short, playful, PG-13 roast (2-3 sentences) of someone named "
        f"{member.display_name}. Keep it funny and lighthearted, not genuinely mean "
        f"or based on real personal info - just silly banter between friends."
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
        await ctx.send("No sry ask daddy jay for help")


keep_alive()
bot.run(TOKEN)