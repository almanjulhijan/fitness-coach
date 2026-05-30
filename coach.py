#!/usr/bin/env python3
"""Strava Training Coach — Discord bot powered by Claude + Strava data."""

import os
import sys
from collections import defaultdict
from pathlib import Path

import anthropic
import discord
from discord.ext import commands
from dotenv import load_dotenv

from strava.auth import get_valid_token
from strava.client import StravaClient

load_dotenv()

MODEL = "claude-haiku-4-5"
KB_DIR = Path("knowledge_base")
MAX_TOKENS = 2048
MAX_HISTORY = 20  # max messages kept per channel


# ── Helpers ────────────────────────────────────────────────────────────────────

def load_knowledge_base():
    if not KB_DIR.exists():
        return ""
    parts = []
    for md_file in sorted(KB_DIR.glob("*.md")):
        content = md_file.read_text(encoding="utf-8").strip()
        if content:
            parts.append("### {}\n\n{}".format(
                md_file.stem.replace("_", " ").title(), content
            ))
    if not parts:
        return ""
    return "## Personal Knowledge Base\n\n" + "\n\n---\n\n".join(parts)


def build_system_prompt(kb_content, activities_summary):
    coach_section = """You are an expert personal running and triathlon coach with deep knowledge of endurance sports training, periodization, race strategy, nutrition, and recovery.

You have access to this athlete's personal profile (in the knowledge base below) and their recent Strava training data. Use both to give specific, data-driven coaching — not generic advice.

## Your role
- Analyze actual training data and surface meaningful patterns (volume trends, HR drift, pace progression, recovery quality)
- Answer questions about training load, race preparation, pacing, gear, nutrition, and injury prevention
- Proactively flag concerns like overtraining, insufficient recovery, or training imbalances
- Help the athlete set realistic goals and build toward them step by step
- Be honest — if something in the data looks concerning, say so clearly

## Communication style
- Conversational but precise — reference specific activities, dates, and numbers from the data
- Keep responses focused and actionable; avoid walls of generic text
- Discord formatting: use **bold** for emphasis, keep responses under ~400 words unless detail is needed
- Ask clarifying questions when context matters
- Use metric units unless the athlete's profile specifies otherwise"""

    sections = [coach_section]
    if kb_content:
        sections.append(kb_content)
    if activities_summary:
        sections.append(activities_summary)

    combined = "\n\n---\n\n".join(sections)
    return [{"type": "text", "text": combined, "cache_control": {"type": "ephemeral"}}]


def load_strava_data(client_id, client_secret):
    tokens = get_valid_token(client_id, client_secret)
    strava = StravaClient(tokens["access_token"])
    athlete = strava.get_athlete()
    activities = strava.get_activities(days=30)
    name = "{} {}".format(athlete.get("firstname", ""), athlete.get("lastname", "")).strip()
    print("Athlete: {}".format(name) if name else "Athlete loaded")
    print("Loaded {} activities from the last 30 days.".format(len(activities)))
    return strava.format_activities_summary(activities, athlete=athlete)


# ── Discord bot ────────────────────────────────────────────────────────────────

def run_bot(discord_token, client_id, client_secret, anthropic_key):
    claude = anthropic.Anthropic(api_key=anthropic_key)

    print("\n=== Strava Training Coach (Discord) ===")
    print("Loading Strava data...")
    activities_summary = load_strava_data(client_id, client_secret)
    kb_content = load_knowledge_base()
    if not kb_content:
        print("Tip: Edit knowledge_base/about_me.md to give your coach personal context.")

    # Shared state — mutable so refresh command can update in place
    state = {
        "system": build_system_prompt(kb_content, activities_summary)
    }

    # Per-channel conversation history: {channel_id: [{"role": ..., "content": ...}]}
    history = defaultdict(list)

    intents = discord.Intents.default()
    intents.message_content = True
    bot = commands.Bot(command_prefix="!", intents=intents)

    @bot.event
    async def on_ready():
        print("Bot online as {}".format(bot.user))
        print("Mention @{} to chat. Use !refresh to reload Strava data.".format(bot.user.name))

    @bot.command(name="refresh")
    async def refresh(ctx):
        """Reload Strava data and reset channel history."""
        async with ctx.typing():
            try:
                new_summary = load_strava_data(client_id, client_secret)
                new_kb = load_knowledge_base()
                state["system"] = build_system_prompt(new_kb, new_summary)
                history[ctx.channel.id].clear()
                await ctx.send("✅ Strava data refreshed and conversation history cleared!")
            except Exception as e:
                await ctx.send("❌ Refresh failed: {}".format(e))

    @bot.command(name="clear")
    async def clear_history(ctx):
        """Clear conversation history for this channel."""
        history[ctx.channel.id].clear()
        await ctx.send("🧹 Conversation history cleared for this channel.")

    @bot.event
    async def on_message(msg):
        # Let commands (! prefix) be handled by the command processor
        await bot.process_commands(msg)

        # Only respond when mentioned, and ignore the bot's own messages
        if msg.author.bot:
            return
        if bot.user not in msg.mentions:
            return

        # Strip the mention from the message
        user_text = msg.content
        for mention in msg.mentions:
            user_text = user_text.replace("<@{}>".format(mention.id), "").replace(
                "<@!{}>".format(mention.id), ""
            )
        user_text = user_text.strip()

        if not user_text:
            await msg.channel.send("Hey! Ask me anything about your training 🏃")
            return

        channel_history = history[msg.channel.id]
        channel_history.append({"role": "user", "content": user_text})

        # Trim history to stay within limits
        if len(channel_history) > MAX_HISTORY:
            channel_history[:] = channel_history[-MAX_HISTORY:]

        async with msg.channel.typing():
            try:
                response = claude.messages.create(
                    model=MODEL,
                    max_tokens=MAX_TOKENS,
                    system=state["system"],
                    messages=channel_history,
                )
                reply = ""
                for block in response.content:
                    if block.type == "text":
                        reply += block.text

                channel_history.append({"role": "assistant", "content": reply})

                # Discord has a 2000 char limit per message — split if needed
                if len(reply) <= 2000:
                    await msg.channel.send(reply)
                else:
                    chunks = [reply[i:i+1900] for i in range(0, len(reply), 1900)]
                    for chunk in chunks:
                        await msg.channel.send(chunk)

            except anthropic.APIError as e:
                channel_history.pop()  # remove failed user message
                await msg.channel.send("❌ API error: {}".format(e))

    bot.run(discord_token)


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    client_id = os.getenv("STRAVA_CLIENT_ID", "").strip()
    client_secret = os.getenv("STRAVA_CLIENT_SECRET", "").strip()
    anthropic_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    discord_token = os.getenv("DISCORD_BOT_TOKEN", "").strip()

    missing = []
    if not client_id:
        missing.append("STRAVA_CLIENT_ID")
    if not client_secret:
        missing.append("STRAVA_CLIENT_SECRET")
    if not anthropic_key:
        missing.append("ANTHROPIC_API_KEY")
    if not discord_token:
        missing.append("DISCORD_BOT_TOKEN")

    if missing:
        print("Error: Missing required environment variables:")
        for var in missing:
            print("  - {}".format(var))
        print("\nAdd them to your .env file.")
        sys.exit(1)

    run_bot(discord_token, client_id, client_secret, anthropic_key)


if __name__ == "__main__":
    main()
