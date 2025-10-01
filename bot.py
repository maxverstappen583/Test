# bot.py
import os
import json
import asyncio
import base64
import threading
from datetime import datetime
from pathlib import Path

import discord
from discord.ext import commands
from discord import option
from flask import Flask
from dotenv import load_dotenv
import random
import re

# -------------------------
# Load environment variables
# -------------------------
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))  # Put your Discord ID in .env as OWNER_ID
PORT = int(os.getenv("PORT", "8080"))

if not TOKEN:
    raise SystemExit("DISCORD_TOKEN is not set in .env")

# -------------------------
# Data persistence helpers
# -------------------------
DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)

def load_json(fn, default):
    fp = DATA_DIR / fn
    if fp.exists():
        try:
            return json.loads(fp.read_text(encoding="utf-8"))
        except Exception:
            return default
    else:
        fp.write_text(json.dumps(default, indent=2), encoding="utf-8")
        return default

def save_json(fn, data):
    fp = DATA_DIR / fn
    fp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

# persistent files
SETTINGS = load_json("settings.json", {
    "log_channel_id": None,
    "welcome_channel_id": None,
    "welcome_message": "Welcome {mention} to {guild}! You are member #{count}.",
    "blocked_words": [],   # list of blocked words
})
AVATAR_HISTORY = load_json("avatar_history.json", {})  # {user_id: [urls...]}
REACT_ROLES = load_json("react_roles.json", {})  # {message_id: [{emoji, role_id}, ...]}
LOGS = load_json("logs.json", [])  # list of log dicts
AFK = load_json("afk.json", {})  # {user_id: {"reason": str, "since": timestamp}}

# -------------------------
# Flask keepalive
# -------------------------
app = Flask("bot_keepalive")

@app.route("/")
def home():
    return "Bot is running."

def run_flask():
    app.run(host="0.0.0.0", port=PORT)

def start_keepalive():
    t = threading.Thread(target=run_flask, daemon=True)
    t.start()

# -------------------------
# Bot setup
# -------------------------
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds = True

bot = commands.Bot(command_prefix="?", intents=intents)

# small helper: is admin/owner
def is_owner_or_admin(user: discord.Member):
    if user.id == OWNER_ID:
        return True
    try:
        return user.guild_permissions.administrator
    except Exception:
        return False

def add_log(entry: dict):
    # keep only last 1000 logs to avoid huge files
    LOGS.append(entry)
    if len(LOGS) > 1000:
        del LOGS[:-1000]
    save_json("logs.json", LOGS)

async def send_to_log_channel(guild: discord.Guild, embed: discord.Embed):
    cid = SETTINGS.get("log_channel_id")
    if not cid:
        return
    ch = guild.get_channel(cid) or bot.get_channel(cid)
    if ch:
        try:
            await ch.send(embed=embed)
        except Exception:
            pass

# -------------------------
# Events
# -------------------------
@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} ({bot.user.id})")
    start_keepalive()

@bot.event
async def on_user_update(before: discord.User, after: discord.User):
    # Track avatar history (persist across restarts)
    try:
        if before.avatar != after.avatar:
            uid = str(after.id)
            url = after.display_avatar.url
            lst = AVATAR_HISTORY.get(uid, [])
            if not lst or lst[-1] != url:
                lst.append(url)
                AVATAR_HISTORY[uid] = lst
                save_json("avatar_history.json", AVATAR_HISTORY)
    except Exception:
        pass

@bot.event
async def on_member_join(member: discord.Member):
    # welcome message
    wid = SETTINGS.get("welcome_channel_id")
    if wid:
        ch = member.guild.get_channel(wid)
        if ch:
            msg = SETTINGS.get("welcome_message", "Welcome {mention} to {guild}! You are member #{count}.")
            filled = msg.format(mention=member.mention, guild=member.guild.name, count=member.guild.member_count)
            try:
                await ch.send(filled)
            except Exception:
                pass

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    # AFK mention handling: if someone mentions an AFK user inform them
    for u in message.mentions:
        info = AFK.get(str(u.id))
        if info:
            reason = info.get("reason","AFK")
            since = datetime.fromtimestamp(info.get("since", 0)).strftime("%Y-%m-%d %H:%M:%S")
            await message.channel.send(f"🔕 {u.display_name} is AFK: {reason} (since {since})")

    # automod: blocked words detection (normalize incoming message)
    content_norm = re.sub(r"[^A-Za-z0-9]", "", message.content).lower()
    for bw in SETTINGS.get("blocked_words", []):
        bw_norm = re.sub(r"[^A-Za-z0-9]", "", bw).lower()
        if bw_norm and bw_norm in content_norm:
            # skip admins
            try:
                if is_owner_or_admin(message.author):
                    break
            except Exception:
                pass
            # delete and warn
            try:
                await message.delete()
            except Exception:
                pass
            try:
                await message.channel.send(f"{message.author.mention} This word is not allowed here.")
            except Exception:
                pass
            # log
            entry = {
                "ts": datetime.utcnow().isoformat(),
                "type": "automod_block",
                "guild_id": message.guild.id if message.guild else None,
                "channel_id": message.channel.id,
                "user_id": message.author.id,
                "content": message.content,
                "matched": bw
            }
            add_log(entry)
            return  # stop processing this message further

    await bot.process_commands(message)

@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    # reaction role handling
    try:
        mid = str(payload.message_id)
        if mid in REACT_ROLES:
            rows = REACT_ROLES[mid]
            for rr in rows:
                if str(rr["emoji"]) == str(payload.emoji):
                    guild = bot.get_guild(payload.guild_id)
                    role = guild.get_role(rr["role_id"])
                    member = guild.get_member(payload.user_id)
                    if role and member:
                        await member.add_roles(role, reason="Reaction role added")
    except Exception:
        pass

@bot.event
async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent):
    try:
        mid = str(payload.message_id)
        if mid in REACT_ROLES:
            rows = REACT_ROLES[mid]
            for rr in rows:
                if str(rr["emoji"]) == str(payload.emoji):
                    guild = bot.get_guild(payload.guild_id)
                    role = guild.get_role(rr["role_id"])
                    member = guild.get_member(payload.user_id)
                    if role and member:
                        await member.remove_roles(role, reason="Reaction role removed")
    except Exception:
        pass

# -------------------------
# Utilities / Helpers
# -------------------------
def make_basic_embed(title="Info", color=discord.Color.blurple()):
    return discord.Embed(title=title, color=color, timestamp=datetime.utcnow())

def record_command_usage(ctx, name, extra=""):
    entry = {
        "ts": datetime.utcnow().isoformat(),
        "user_id": getattr(ctx.author, "id", None),
        "user_name": str(getattr(ctx.author, "name", None)),
        "guild_id": getattr(getattr(ctx, "guild", None), "id", None),
        "channel_id": getattr(getattr(ctx, "channel", None), "id", None),
        "channel_name": getattr(getattr(ctx, "channel", None), "name", None),
        "command": name,
        "extra": extra
    }
    add_log(entry)
    # also send embed to log channel if set
    emb = discord.Embed(title=f"Command: {name}", color=discord.Color.dark_gray(), timestamp=datetime.utcnow())
    emb.add_field(name="User", value=f"{ctx.author} ({ctx.author.id})", inline=False)
    emb.add_field(name="Guild", value=f"{getattr(getattr(ctx,'guild',None),'name', 'DM')} ({entry['guild_id']})", inline=True)
    emb.add_field(name="Channel", value=f"{getattr(getattr(ctx,'channel',None),'name','-')} ({entry['channel_id']})", inline=True)
    if extra:
        emb.add_field(name="Extra", value=str(extra), inline=False)
    guild = getattr(ctx, "guild", None)
    if guild:
        asyncio.create_task(send_to_log_channel(guild, emb))

# -------------------------
# Commands
# -------------------------

# ---- ping/pong
@bot.slash_command(description="Check bot latency")
async def ping(ctx):
    await ctx.respond(f"Pong! {round(bot.latency*1000)}ms")
    record_command_usage(ctx, "ping")

@bot.command()
async def pong(ctx):
    await ctx.send(f"Pong! {round(bot.latency*1000)}ms")
    record_command_usage(ctx, "pong")

# ---- serverinfo (/ and ?)
@bot.slash_command(description="Get information about this server")
async def serverinfo(ctx):
    g = ctx.guild
    emb = make_basic_embed(f"Server Info — {g.name}")
    emb.set_thumbnail(url=g.icon.url if g.icon else discord.Embed.Empty)
    emb.add_field(name="Owner", value=f"{g.owner} ({g.owner.id})", inline=True)
    emb.add_field(name="Members", value=str(g.member_count), inline=True)
    emb.add_field(name="Roles", value=str(len(g.roles)), inline=True)
    emb.add_field(name="Channels", value=str(len(g.channels)), inline=True)
    emb.add_field(name="Created", value=g.created_at.strftime("%Y-%m-%d %H:%M:%S"), inline=False)
    await ctx.respond(embed=emb)
    record_command_usage(ctx, "serverinfo")

@bot.command(name="serverinfo")
async def serverinfo_prefix(ctx):
    g = ctx.guild
    emb = make_basic_embed(f"Server Info — {g.name}", color=discord.Color.green())
    emb.set_thumbnail(url=g.icon.url if g.icon else discord.Embed.Empty)
    emb.add_field(name="Owner", value=f"{g.owner} ({g.owner.id})", inline=True)
    emb.add_field(name="Members", value=str(g.member_count), inline=True)
    emb.add_field(name="Roles", value=str(len(g.roles)), inline=True)
    emb.add_field(name="Channels", value=str(len(g.channels)), inline=True)
    emb.add_field(name="Created", value=g.created_at.strftime("%Y-%m-%d %H:%M:%S"), inline=False)
    await ctx.send(embed=emb)
    record_command_usage(ctx, "serverinfo")

# ---- avatar & avatarhistory
@bot.slash_command(description="Get a user's avatar")
@option("user", discord.Member, description="User to fetch (leave empty for yourself)", required=False)
async def avatar(ctx, user: discord.Member = None):
    target = user or ctx.author
    emb = make_basic_embed(f"Avatar — {target}")
    emb.set_image(url=target.display_avatar.url)
    await ctx.respond(embed=emb)
    record_command_usage(ctx, "avatar", f"target={target.id}")

@bot.command()
async def avatar(ctx, member: discord.Member = None):
    member = member or ctx.author
    emb = make_basic_embed(f"Avatar — {member}")
    emb.set_image(url=member.display_avatar.url)
    await ctx.send(embed=emb)
    record_command_usage(ctx, "avatar", f"target={member.id}")

@bot.slash_command(description="Show a user's avatar history (tracked)")
@option("user", discord.User, description="User to check", required=False)
async def avatarhistory(ctx, user: discord.User = None):
    target = user or ctx.author
    arr = AVATAR_HISTORY.get(str(target.id), [])
    emb = make_basic_embed(f"Avatar History — {target}")
    if not arr:
        emb.description = "No avatar history tracked for this user."
    else:
        for i, url in enumerate(arr[-10:], start=1):
            emb.add_field(name=f"#{i}", value=url, inline=False)
        # show last avatar as image
        emb.set_image(url=arr[-1])
    await ctx.respond(embed=emb)
    record_command_usage(ctx, "avatarhistory", f"target={target.id}")

@bot.command()
async def avatarhistory(ctx, user: discord.User = None):
    user = user or ctx.author
    arr = AVATAR_HISTORY.get(str(user.id), [])
    emb = make_basic_embed(f"Avatar History — {user}")
    if not arr:
        emb.description = "No avatar history tracked for this user."
    else:
        for i, url in enumerate(arr[-10:], start=1):
            emb.add_field(name=f"#{i}", value=url, inline=False)
        emb.set_image(url=arr[-1])
    await ctx.send(embed=emb)
    record_command_usage(ctx, "avatarhistory", f"target={user.id}")

# ---- banner
@bot.slash_command(description="Get a user's banner")
@option("user", discord.User, description="User to fetch banner (optional)", required=False)
async def banner(ctx, user: discord.User = None):
    target = user or ctx.author
    fuser = await bot.fetch_user(target.id)
    if fuser.banner:
        emb = make_basic_embed(f"Banner — {target}")
        emb.set_image(url=fuser.banner.url)
    else:
        emb = make_basic_embed("Banner")
        emb.description = "This user has no profile banner."
    await ctx.respond(embed=emb)
    record_command_usage(ctx, "banner", f"target={target.id}")

@bot.command()
async def banner(ctx, user: discord.User = None):
    user = user or ctx.author
    fuser = await bot.fetch_user(user.id)
    if fuser.banner:
        emb = make_basic_embed(f"Banner — {user}")
        emb.set_image(url=fuser.banner.url)
    else:
        emb = make_basic_embed("Banner")
        emb.description = "This user has no profile banner."
    await ctx.send(embed=emb)
    record_command_usage(ctx, "banner", f"target={user.id}")

# ---- base64 encode/decode
@bot.slash_command(description="Convert text to/from Base64")
@option("mode", str, description="encode or decode", required=True, choices=["encode","decode"])
@option("text", str, description="Text to convert", required=True)
async def base64cmd(ctx, mode: str, text: str):
    if mode == "encode":
        out = base64.b64encode(text.encode()).decode()
    else:
        try:
            out = base64.b64decode(text.encode()).decode()
        except Exception:
            out = "❗ Failed to decode base64 (invalid input)."
    emb = make_basic_embed(f"Base64 {mode}")
    emb.add_field(name="Result", value=f"```\n{out}\n```", inline=False)
    await ctx.respond(embed=emb)
    record_command_usage(ctx, "base64", f"{mode}")

@bot.command(name="base64")
async def base64_prefix(ctx, mode: str, *, text: str):
    if mode not in ["encode","decode"]:
        await ctx.send("Usage: `?base64 encode|decode <text>`")
        return
    if mode == "encode":
        out = base64.b64encode(text.encode()).decode()
    else:
        try:
            out = base64.b64decode(text.encode()).decode()
        except Exception:
            out = "❗ Failed to decode base64 (invalid input)."
    emb = make_basic_embed(f"Base64 {mode}")
    emb.add_field(name="Result", value=f"```\n{out}\n```", inline=False)
    await ctx.send(embed=emb)
    record_command_usage(ctx, "base64", f"{mode}")

# ---- joined
@bot.slash_command(description="See when a user joined the server")
@option("user", discord.Member, description="User to check (default: you)", required=False)
async def joined(ctx, user: discord.Member = None):
    member = user or ctx.author
    emb = make_basic_embed(f"Join Date — {member}")
    emb.add_field(name="Joined", value=member.joined_at.strftime("%Y-%m-%d %H:%M:%S") if member.joined_at else "Unknown", inline=False)
    emb.add_field(name="Account Created", value=member.created_at.strftime("%Y-%m-%d %H:%M:%S"), inline=False)
    await ctx.respond(embed=emb)
    record_command_usage(ctx, "joined", f"target={member.id}")

@bot.command()
async def joined(ctx, member: discord.Member = None):
    member = member or ctx.author
    emb = make_basic_embed(f"Join Date — {member}", color=discord.Color.teal())
    emb.add_field(name="Joined", value=member.joined_at.strftime("%Y-%m-%d %H:%M:%S") if member.joined_at else "Unknown", inline=False)
    emb.add_field(name="Account Created", value=member.created_at.strftime("%Y-%m-%d %H:%M:%S"), inline=False)
    await ctx.send(embed=emb)
    record_command_usage(ctx, "joined", f"target={member.id}")

# ---- permissions
@bot.slash_command(description="List a user's server permissions")
@option("user", discord.Member, description="User (optional)", required=False)
async def permissions(ctx, user: discord.Member = None):
    m = user or ctx.author
    perms = [n.replace("_"," ").title() for n,v in m.guild_permissions if v] if False else None
    # simpler: stringify guild_permissions
    perm_txt = ", ".join([p[0] for p in m.guild_permissions if p[1]])
    emb = make_basic_embed(f"Permissions — {m}")
    emb.description = perm_txt or "No special permissions."
    await ctx.respond(embed=emb)
    record_command_usage(ctx, "permissions", f"target={m.id}")

@bot.command()
async def permissions(ctx, member: discord.Member = None):
    member = member or ctx.author
    perm_txt = ", ".join([p[0] for p in member.guild_permissions if p[1]])
    emb = make_basic_embed(f"Permissions — {member}", color=discord.Color.dark_gold())
    emb.description = perm_txt or "No special permissions."
    await ctx.send(embed=emb)
    record_command_usage(ctx, "permissions", f"target={member.id}")

# ---- roles (list roles a user has)
@bot.slash_command(description="List all roles a user has in the server")
@option("user", discord.Member, description="User to check (optional)", required=False)
async def roles(ctx, user: discord.Member = None):
    m = user or ctx.author
    role_list = [r.mention for r in m.roles if r.name != "@everyone"]
    emb = make_basic_embed(f"Roles — {m}")
    emb.description = ", ".join(role_list) if role_list else "No roles."
    await ctx.respond(embed=emb)
    record_command_usage(ctx, "roles", f"target={m.id}")

@bot.command()
async def roles(ctx, member: discord.Member = None):
    member = member or ctx.author
    role_list = [r.mention for r in member.roles if r.name != "@everyone"]
    emb = make_basic_embed(f"Roles — {member}", color=discord.Color.dark_purple())
    emb.description = ", ".join(role_list) if role_list else "No roles."
    await ctx.send(embed=emb)
    record_command_usage(ctx, "roles", f"target={member.id}")

# ---- serverbanner, serverboosts
@bot.slash_command(description="Show the server banner")
async def serverbanner(ctx):
    gid = ctx.guild.id
    g = await bot.fetch_guild(gid)
    if g.banner:
        emb = make_basic_embed("Server Banner")
        emb.set_image(url=g.banner.url)
    else:
        emb = make_basic_embed("Server Banner")
        emb.description = "No server banner."
    await ctx.respond(embed=emb)
    record_command_usage(ctx, "serverbanner")

@bot.slash_command(description="Show server boost info")
async def serverboosts(ctx):
    g = ctx.guild
    emb = make_basic_embed("Server Boosts")
    emb.add_field(name="Boost Count", value=str(getattr(g, "premium_subscription_count", 0)))
    emb.add_field(name="Boost Tier", value=str(getattr(g, "premium_tier", "Unknown")))
    await ctx.respond(embed=emb)
    record_command_usage(ctx, "serverboosts")

# ---- profileinfo
@bot.slash_command(description="Get detailed profile info for a user")
@option("user", discord.User, description="User to fetch", required=False)
async def profileinfo(ctx, user: discord.User = None):
    user = user or ctx.author
    fetched = await bot.fetch_user(user.id)
    emb = make_basic_embed(f"Profile — {user}")
    emb.set_thumbnail(url=fetched.display_avatar.url)
    emb.add_field(name="ID", value=str(user.id), inline=True)
    emb.add_field(name="Bot?", value=str(user.bot), inline=True)
    emb.add_field(name="Created", value=user.created_at.strftime("%Y-%m-%d %H:%M:%S"), inline=False)
    if isinstance(user, discord.Member):
        emb.add_field(name="Joined", value=user.joined_at.strftime("%Y-%m-%d %H:%M:%S") if user.joined_at else "Unknown", inline=False)
    await ctx.respond(embed=emb)
    record_command_usage(ctx, "profileinfo", f"target={user.id}")

@bot.command()
async def profileinfo(ctx, user: discord.User = None):
    user = user or ctx.author
    fetched = await bot.fetch_user(user.id)
    emb = make_basic_embed(f"Profile — {user}", color=discord.Color.orange())
    emb.set_thumbnail(url=fetched.display_avatar.url)
    emb.add_field(name="ID", value=str(user.id), inline=True)
    emb.add_field(name="Bot?", value=str(user.bot), inline=True)
    emb.add_field(name="Created", value=user.created_at.strftime("%Y-%m-%d %H:%M:%S"), inline=False)
    if isinstance(user, discord.Member):
        emb.add_field(name="Joined", value=user.joined_at.strftime("%Y-%m-%d %H:%M:%S") if user.joined_at else "Unknown", inline=False)
    await ctx.send(embed=emb)
    record_command_usage(ctx, "profileinfo", f"target={user.id}")

# ---- 8ball
EIGHT_BALL = ["Yes.", "No.", "Maybe.", "Ask again later.", "Definitely.", "I doubt it."]
@bot.slash_command(description="Ask the magic 8ball a question")
@option("question", str, description="Your question", required=True)
async def _8ball(ctx, question: str):
    await ctx.respond(random.choice(EIGHT_BALL))
    record_command_usage(ctx, "8ball", question)

@bot.command(name="8ball")
async def eightball(ctx, *, question: str):
    await ctx.send(random.choice(EIGHT_BALL))
    record_command_usage(ctx, "8ball", question)

# ---- afk
@bot.slash_command(description="Set your AFK status")
@option("reason", str, description="AFK reason", required=False)
async def afk(ctx, reason: str = "AFK"):
    AFK[str(ctx.author.id)] = {"reason": reason, "since": datetime.utcnow().timestamp()}
    save_json("afk.json", AFK)
    await ctx.respond(f"Set AFK: {reason}")
    record_command_usage(ctx, "afk", reason)

@bot.command()
async def afk(ctx, *, reason: str = "AFK"):
    AFK[str(ctx.author.id)] = {"reason": reason, "since": datetime.utcnow().timestamp()}
    save_json("afk.json", AFK)
    await ctx.send(f"Set AFK: {reason}")
    record_command_usage(ctx, "afk", reason)

# ---- setlog / disable_log_channel
@bot.slash_command(description="Set the audit/logging channel for this server")
@option("channel", discord.TextChannel, description="Channel to post logs in", required=True)
async def setlog(ctx, channel: discord.TextChannel):
    if not is_owner_or_admin(ctx.author):
        await ctx.respond("You must be an admin to run that.")
        return
    SETTINGS["log_channel_id"] = channel.id
    save_json("settings.json", SETTINGS)
    await ctx.respond(f"Log channel set to {channel.mention}")
    record_command_usage(ctx, "setlog", f"channel={channel.id}")

@bot.slash_command(description="Disable log channel posting")
async def disable_log_channel(ctx):
    if not is_owner_or_admin(ctx.author):
        await ctx.respond("You must be an admin to run that.")
        return
    SETTINGS["log_channel_id"] = None
    save_json("settings.json", SETTINGS)
    await ctx.respond("Log channel disabled.")
    record_command_usage(ctx, "disable_log_channel")

# ---- welcome setting
@bot.slash_command(description="Set welcome channel and message")
@option("channel", discord.TextChannel, description="Channel to send welcome messages", required=True)
@option("message", str, description="Message template (use {mention}, {guild}, {count})", required=False)
async def welcomer(ctx, channel: discord.TextChannel, message: str = None):
    if not is_owner_or_admin(ctx.author):
        await ctx.respond("You must be an admin to run that.")
        return
    SETTINGS["welcome_channel_id"] = channel.id
    if message:
        SETTINGS["welcome_message"] = message
    save_json("settings.json", SETTINGS)
    await ctx.respond(f"Welcome channel set to {channel.mention}.")
    record_command_usage(ctx, "welcomer", f"channel={channel.id}")

# ---- blocked words (automod)
@bot.slash_command(description="Add a blocked word (admin only)")
@option("word", str, description="Word to block", required=True)
async def add_blocked_word(ctx, word: str):
    if not is_owner_or_admin(ctx.author):
        await ctx.respond("Admin only.")
        return
    if word.lower() in (w.lower() for w in SETTINGS.get("blocked_words", [])):
        await ctx.respond("That word is already blocked.")
        return
    SETTINGS["blocked_words"].append(word)
    save_json("settings.json", SETTINGS)
    await ctx.respond(f"Blocked word added: `{word}`")
    record_command_usage(ctx, "add_blocked_word", word)

@bot.slash_command(description="Remove a blocked word (admin only)")
@option("word", str, description="Word to remove", required=True)
async def remove_blocked_word(ctx, word: str):
    if not is_owner_or_admin(ctx.author):
        await ctx.respond("Admin only.")
        return
    SETTINGS["blocked_words"] = [w for w in SETTINGS.get("blocked_words", []) if w.lower() != word.lower()]
    save_json("settings.json", SETTINGS)
    await ctx.respond(f"Blocked word removed: `{word}`")
    record_command_usage(ctx, "remove_blocked_word", word)

@bot.slash_command(description="Show blocked words")
async def show_blocked_words(ctx):
    lst = SETTINGS.get("blocked_words", [])
    emb = make_basic_embed("Blocked Words")
    emb.description = ", ".join(f"`{w}`" for w in lst) if lst else "No blocked words."
    await ctx.respond(embed=emb)
    record_command_usage(ctx, "show_blocked_words")

# ---- reactrolecreate (slash with options) + storage
@bot.slash_command(description="Create a reaction role message (admin)")
@option("channel", discord.TextChannel, description="Channel to post the message", required=True)
@option("message", str, description="Message content", required=True)
@option("role", discord.Role, description="Role to give", required=True)
@option("emoji", str, description="Emoji to use (unicode or custom like <:name:id>)", required=True)
async def reactrolecreate(ctx, channel: discord.TextChannel, message: str, role: discord.Role, emoji: str):
    if not is_owner_or_admin(ctx.author):
        await ctx.respond("Admin only.")
        return
    sent = await channel.send(message)
    try:
        await sent.add_reaction(emoji)
    except Exception:
        # try convert emoji by name => user may pass literal emoji
        pass
    mid = str(sent.id)
    entry = REACT_ROLES.get(mid, [])
    entry.append({"emoji": emoji, "role_id": role.id})
    REACT_ROLES[mid] = entry
    save_json("react_roles.json", REACT_ROLES)
    await ctx.respond(f"Reaction role created in {channel.mention}")
    record_command_usage(ctx, "reactrolecreate", f"mid={mid}, role={role.id}, emoji={emoji}")

# ---- purge (delete messages)
@bot.slash_command(description="Delete a number of messages from this channel (Admin only)")
@option("amount", int, description="Number of messages to delete (2-100)", required=True)
async def purge(ctx, amount: int):
    if not is_owner_or_admin(ctx.author) and not ctx.author.guild_permissions.manage_messages:
        await ctx.respond("You need Manage Messages permission to use this.")
        return
    amount = max(2, min(amount, 100))
    deleted = await ctx.channel.purge(limit=amount)
    await ctx.respond(f"Deleted {len(deleted)} messages.", ephemeral=True)
    record_command_usage(ctx, "purge", f"amount={amount}")

@bot.command()
@commands.has_permissions(manage_messages=True)
async def purge(ctx, amount: int):
    amount = max(2, min(amount, 100))
    deleted = await ctx.channel.purge(limit=amount)
    await ctx.send(f"Deleted {len(deleted)} messages.", delete_after=8)
    record_command_usage(ctx, "purge", f"amount={amount}")

# ---- mute role add/remove
@bot.slash_command(description="Add or remove the mute role to a user (Admin only)")
@option("member", discord.Member, description="Member to mute/unmute", required=True)
@option("action", str, description="add or remove", required=True, choices=["add","remove"])
async def muterole(ctx, member: discord.Member, action: str):
    if not is_owner_or_admin(ctx.author) and not ctx.author.guild_permissions.manage_roles:
        await ctx.respond("You do not have permission to do that.")
        return
    guild = ctx.guild
    role = discord.utils.get(guild.roles, name="Muted")
    if not role:
        # create role
        role = await guild.create_role(name="Muted", reason="Muted role created by bot")
        # attempt to set channel overwrite to deny send_messages
        for ch in guild.text_channels:
            try:
                await ch.set_permissions(role, send_messages=False, add_reactions=False)
            except Exception:
                pass
    if action == "add":
        await member.add_roles(role, reason="Muted via command")
        await ctx.respond(f"{member.mention} has been muted.")
    else:
        await member.remove_roles(role, reason="Unmuted via command")
        await ctx.respond(f"{member.mention} has been unmuted.")
    record_command_usage(ctx, "muterole", f"{action} {member.id}")

# ---- nuke (channel) and nuke_category and nuke_server (careful)
@bot.slash_command(description="Nuke this channel (Admin only)")
async def nuke(ctx):
    if not is_owner_or_admin(ctx.author):
        await ctx.respond("Owner/Admin only.")
        return
    await ctx.respond("⚠️ Type `confirm` in chat within 20 seconds to proceed with channel nuke.")
    try:
        def check(m):
            return m.author == ctx.author and m.channel == ctx.channel and m.content.lower() == "confirm"
        msg = await bot.wait_for("message", check=check, timeout=20)
    except asyncio.TimeoutError:
        await ctx.channel.send("Nuke cancelled (timeout).")
        return
    # clone and delete
    new = await ctx.channel.clone(reason=f"Nuked by {ctx.author}")
    try:
        await ctx.channel.delete()
    except Exception:
        pass
    await new.send("Channel nuked (recreated).")
    record_command_usage(ctx, "nuke")

@bot.slash_command(description="Nuke the whole category (Admin only)")
@option("category", discord.CategoryChannel, description="Category to nuke", required=True)
async def nuke_category(ctx, category: discord.CategoryChannel):
    if not is_owner_or_admin(ctx.author):
        await ctx.respond("Owner/Admin only.")
        return
    await ctx.respond("⚠️ Type `confirm category` in chat within 20 seconds to proceed.")
    try:
        def check(m):
            return m.author == ctx.author and m.channel == ctx.channel and m.content.lower() == "confirm category"
        msg = await bot.wait_for("message", check=check, timeout=20)
    except asyncio.TimeoutError:
        await ctx.channel.send("Cancelled (timeout).")
        return
    # clone channels then delete originals
    channels = list(category.channels)
    for ch in channels:
        try:
            await ch.clone(category=category, reason=f"Nuked by {ctx.author}")
            await ch.delete()
        except Exception:
            pass
    await ctx.send("Category nuked and recreated (where possible).")
    record_command_usage(ctx, "nuke_category", f"category={category.id}")

@bot.slash_command(description="Nuke server (owner only, very destructive!)")
async def nuke_server(ctx):
    if ctx.author.id != OWNER_ID:
        await ctx.respond("Owner only.")
        return
    await ctx.respond("⚠️ This will attempt to delete channels/roles. Type the SERVER NAME to confirm within 25s.")
    try:
        def check(m):
            return m.author == ctx.author and m.channel == ctx.channel and m.content == ctx.guild.name
        await bot.wait_for("message", check=check, timeout=25)
    except asyncio.TimeoutError:
        await ctx.channel.send("Cancelled.")
        return
    guild = ctx.guild
    # try to delete channels
    for ch in list(guild.channels):
        try:
            await ch.delete()
        except Exception:
            pass
    # try to delete roles (skip @everyone)
    for r in list(guild.roles)[1:]:
        try:
            await r.delete()
        except Exception:
            pass
    await ctx.channel.send("Nuke attempted. Some items may remain (permissions).")
    record_command_usage(ctx, "nuke_server")

# ---- react role management commands: list and remove
@bot.slash_command(description="List reaction role messages")
async def list_react_roles(ctx):
    items = []
    for mid, arr in REACT_ROLES.items():
        items.append(f"Message ID {mid}: {len(arr)} roles")
    emb = make_basic_embed("React Roles")
    emb.description = "\n".join(items) if items else "No reaction roles configured."
    await ctx.respond(embed=emb)
    record_command_usage(ctx, "list_react_roles")

@bot.slash_command(description="Remove react roles entry (admin)")
@option("message_id", str, description="Message ID to remove", required=True)
async def remove_react_roles(ctx, message_id: str):
    if not is_owner_or_admin(ctx.author):
        await ctx.respond("Admin only.")
        return
    if message_id in REACT_ROLES:
        del REACT_ROLES[message_id]
        save_json("react_roles.json", REACT_ROLES)
        await ctx.respond("Removed.")
    else:
        await ctx.respond("Message ID not found.")
    record_command_usage(ctx, "remove_react_roles", message_id)

# ---- logs & usage (shows last 10)
@bot.slash_command(description="Show recent logs (admin)")
async def logs(ctx):
    if not is_owner_or_admin(ctx.author):
        await ctx.respond("Admin only.")
        return
    out = LOGS[-10:]
    emb = make_basic_embed("Recent Logs")
    if not out:
        emb.description = "No logs yet."
    else:
        for l in reversed(out):
            t = l.get("ts","")
            user = l.get("user_name") or str(l.get("user_id"))
            command = l.get("command", l.get("type",""))
            emb.add_field(name=f"{t} — {user}", value=f"{command} in channel {l.get('channel_name')} ({l.get('channel_id')})", inline=False)
    await ctx.respond(embed=emb)
    record_command_usage(ctx, "logs")

@bot.slash_command(description="Show a user's command usage")
@option("user", discord.User, description="User to show", required=False)
async def usage(ctx, user: discord.User = None):
    if not is_owner_or_admin(ctx.author):
        await ctx.respond("Admin only.")
        return
    target = user or ctx.author
    items = [l for l in LOGS if l.get("user_id") == target.id]
    emb = make_basic_embed(f"Usage for {target}")
    if not items:
        emb.description = "No usage found."
    else:
        for i, l in enumerate(reversed(items[-20:]), start=1):
            emb.add_field(name=f"{i}. {l.get('ts')}", value=f"{l.get('command')} in {l.get('channel_name')}", inline=False)
    await ctx.respond(embed=emb)
    record_command_usage(ctx, "usage", f"target={target.id}")

# ---- ask (simple placeholder)
@bot.slash_command(description="Ask the bot something (simple responses)")
@option("question", str, description="Question to ask", required=True)
async def ask(ctx, question: str):
    # This is a placeholder simple-answer system. You can wire an external AI by adding API key handling.
    replies = [
        "Hmm... I think so.",
        "Not sure, try again later.",
        "Yes.",
        "No.",
        "I don't have enough data to answer that now."
    ]
    await ctx.respond(random.choice(replies))
    record_command_usage(ctx, "ask", question)

# -------------------------
# Run
# -------------------------
if __name__ == "__main__":
    # ensure data files saved
    save_json("settings.json", SETTINGS)
    save_json("avatar_history.json", AVATAR_HISTORY)
    save_json("react_roles.json", REACT_ROLES)
    save_json("logs.json", LOGS)
    save_json("afk.json", AFK)

    bot.run(TOKEN)
