#!/usr/bin/env python3
"""Strava Training Coach — Discord bot powered by Claude + Strava data."""

import json
import os
import re
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

TOOLS = [
    {
        "name": "get_goals",
        "description": "Read the athlete's current training goals for the current sport category.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "set_goals",
        "description": (
            "Save the athlete's training goals for the current sport category. "
            "Call this whenever the user sets, updates, adds to, or removes a goal. "
            "Pass the complete updated list of goals."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "goals": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Complete updated list of training goals for this category.",
                }
            },
            "required": ["goals"],
        },
    },
]


# ── Helpers ────────────────────────────────────────────────────────────────────

def _category_slug(category_name):
    slug = category_name.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "_", slug)
    return slug.strip("_")


def _goals_file(category_name):
    return KB_DIR / "goals_{}.md".format(_category_slug(category_name))


def load_goals(category_name):
    f = _goals_file(category_name)
    if not f.exists():
        return []
    lines = f.read_text(encoding="utf-8").splitlines()
    return [l.lstrip("- ").strip() for l in lines if l.strip().startswith("-")]


def save_goals(category_name, goals):
    KB_DIR.mkdir(exist_ok=True)
    content = "# Goals: {}\n\n".format(category_name)
    content += "\n".join("- {}".format(g) for g in goals)
    _goals_file(category_name).write_text(content, encoding="utf-8")


def load_knowledge_base():
    if not KB_DIR.exists():
        return ""
    parts = []
    for md_file in sorted(KB_DIR.glob("*.md")):
        # skip per-category goals files — injected separately per message
        if md_file.name.startswith("goals_"):
            continue
        content = md_file.read_text(encoding="utf-8").strip()
        if content:
            parts.append("### {}\n\n{}".format(
                md_file.stem.replace("_", " ").title(), content
            ))
    if not parts:
        return ""
    return "## Personal Knowledge Base\n\n" + "\n\n---\n\n".join(parts)


def build_system_prompt(kb_content, activities_summary, goals_content=""):
    coach_section = """You are an expert personal running and triathlon coach with deep knowledge of endurance sports training, periodization, race strategy, nutrition, and recovery.

You have access to this athlete's personal profile (in the knowledge base below) and their recent Strava training data. Use both to give specific, data-driven coaching — not generic advice.

## Your role
- Analyze actual training data and surface meaningful patterns (volume trends, HR drift, pace progression, recovery quality)
- Answer questions about training load, race preparation, pacing, gear, nutrition, and injury prevention
- Proactively flag concerns like overtraining, insufficient recovery, or training imbalances
- Help the athlete set realistic goals and build toward them step by step
- Be honest — if something in the data looks concerning, say so clearly

## Goal management
You have tools to read and update the athlete's goals for this sport category. Use them naturally:
- When the athlete mentions a new goal or target, call set_goals with the updated list
- When asked about goals, call get_goals or refer to the goals already in your context
- Always confirm after saving: tell the athlete what you saved

## Communication style
- Conversational but precise — reference specific activities, dates, and numbers from the data
- Keep responses focused and actionable; avoid walls of generic text
- Discord formatting: use **bold** for emphasis, keep responses under ~400 words unless detail is needed
- Ask clarifying questions when context matters
- Use metric units unless the athlete's profile specifies otherwise"""

    sections = [coach_section]
    if kb_content:
        sections.append(kb_content)
    if goals_content:
        sections.append(goals_content)
    if activities_summary:
        sections.append(activities_summary)

    combined = "\n\n---\n\n".join(sections)
    return [{"type": "text", "text": combined, "cache_control": {"type": "ephemeral"}}]


def build_system_prompt_for_category(category_name, kb_content, activities_summary):
    goals = load_goals(category_name) if category_name else []
    goals_content = ""
    if goals:
        goals_content = "## Goals: {}\n\n".format(category_name)
        goals_content += "\n".join("- {}".format(g) for g in goals)
    return build_system_prompt(kb_content, activities_summary, goals_content)


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

    state = {
        "activities_summary": activities_summary,
        "kb_content": kb_content,
    }

    # Per-channel conversation history: {channel_id: [{"role": ..., "content": ...}]}
    history = defaultdict(list)

    intents = discord.Intents.default()
    intents.message_content = True
    bot = commands.Bot(command_prefix="!", intents=intents)

    @bot.event
    async def on_ready():
        print("Bot online as {}".format(bot.user))
        print("Mention @{} to chat.".format(bot.user.name))

    @bot.command(name="refresh")
    async def refresh(ctx):
        """Reload Strava data and reset channel history."""
        async with ctx.typing():
            try:
                new_summary = load_strava_data(client_id, client_secret)
                new_kb = load_knowledge_base()
                state["activities_summary"] = new_summary
                state["kb_content"] = new_kb
                history[ctx.channel.id].clear()
                await ctx.send("Strava data refreshed and conversation history cleared!")
            except Exception as e:
                await ctx.send("Refresh failed: {}".format(e))

    @bot.command(name="clear")
    async def clear_history(ctx):
        """Clear conversation history for this channel."""
        history[ctx.channel.id].clear()
        await ctx.send("Conversation history cleared for this channel.")

    @bot.event
    async def on_message(msg):
        await bot.process_commands(msg)

        if msg.author.bot:
            return
        if bot.user not in msg.mentions:
            return

        user_text = msg.content
        for mention in msg.mentions:
            user_text = user_text.replace("<@{}>".format(mention.id), "").replace(
                "<@!{}>".format(mention.id), ""
            )
        user_text = user_text.strip()

        if not user_text:
            await msg.channel.send("Hey! Ask me anything about your training.")
            return

        category_name = msg.channel.category.name if msg.channel.category else None

        channel_history = history[msg.channel.id]
        channel_history.append({"role": "user", "content": user_text})

        if len(channel_history) > MAX_HISTORY:
            channel_history[:] = channel_history[-MAX_HISTORY:]

        async with msg.channel.typing():
            try:
                system_prompt = build_system_prompt_for_category(
                    category_name, state["kb_content"], state["activities_summary"]
                )

                # Agentic loop — handle tool calls before final reply
                working_messages = list(channel_history)
                reply = ""

                while True:
                    response = claude.messages.create(
                        model=MODEL,
                        max_tokens=MAX_TOKENS,
                        system=system_prompt,
                        messages=working_messages,
                        tools=TOOLS,
                    )

                    if response.stop_reason == "tool_use":
                        # Append assistant message with all content blocks
                        working_messages.append({
                            "role": "assistant",
                            "content": [b.model_dump() for b in response.content],
                        })

                        tool_results = []
                        for block in response.content:
                            if block.type != "tool_use":
                                continue

                            if block.name == "get_goals":
                                goals = load_goals(category_name) if category_name else []
                                result = (
                                    "\n".join("- {}".format(g) for g in goals)
                                    if goals else "No goals set yet."
                                )

                            elif block.name == "set_goals":
                                if category_name:
                                    goals = block.input.get("goals", [])
                                    save_goals(category_name, goals)
                                    # Rebuild system prompt with new goals
                                    system_prompt = build_system_prompt_for_category(
                                        category_name, state["kb_content"], state["activities_summary"]
                                    )
                                    result = "Goals saved for {}.".format(category_name)
                                else:
                                    result = "Cannot save goals: channel has no category."

                            else:
                                result = "Unknown tool."

                            tool_results.append({
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": result,
                            })

                        working_messages.append({"role": "user", "content": tool_results})

                    else:
                        # end_turn — collect text reply
                        for block in response.content:
                            if block.type == "text":
                                reply += block.text
                        break

                channel_history.append({"role": "assistant", "content": reply})

                if len(reply) <= 2000:
                    await msg.channel.send(reply)
                else:
                    chunks = [reply[i:i+1900] for i in range(0, len(reply), 1900)]
                    for chunk in chunks:
                        await msg.channel.send(chunk)

            except anthropic.APIError as e:
                channel_history.pop()
                await msg.channel.send("API error: {}".format(e))

    bot.run(discord_token)


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    expected = ["STRAVA_CLIENT_ID", "STRAVA_CLIENT_SECRET", "ANTHROPIC_API_KEY", "DISCORD_BOT_TOKEN", "STRAVA_REFRESH_TOKEN"]
    for var in expected:
        print("ENV {}: {}".format(var, "SET" if os.getenv(var) else "MISSING"))

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
