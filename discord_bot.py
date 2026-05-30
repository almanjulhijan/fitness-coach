#!/usr/bin/env python3
"""Strava Training Coach — Discord Bot powered by Claude + your Strava data."""

import os
import sys
from collections import defaultdict
from pathlib import Path

import anthropic
import discord
from discord import app_commands
from dotenv import load_dotenv

from strava.auth import get_valid_token
from strava.client import StravaClient

load_dotenv()

MODEL = "claude-haiku-4-5"
KB_DIR = Path("knowledge_base")
MAX_TOKENS = 2048
MAX_HISTORY = 20  # max messages kept per channel

# ── Globals ────────────────────────────────────────────────────────────────────

claude_client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY", ""))
conversation_history: dict[int, list[dict]] = defaultdict(list)
system_prompt: list[dict] = []
cached_activities: list[dict] = []
cached_athlete: dict = {}


# ── Helpers (reused from coach.py) ────────────────────────────────────────────

def load_knowledge_base() -> str:
    if not KB_DIR.exists():
        return ""
    parts = []
    for md_file in sorted(KB_DIR.glob("*.md")):
        content = md_file.read_text(encoding="utf-8").strip()
        if content:
            parts.append(f"### {md_file.stem.replace('_', ' ').title()}\n\n{content}")
    if not parts:
        return ""
    return "## Personal Knowledge Base\n\n" + "\n\n---\n\n".join(parts)


def build_system_prompt(kb_content: str, activities_summary: str) -> list[dict]:
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
- Ask clarifying questions when context matters
- Use metric units unless the athlete's profile specifies otherwise"""

    sections = [coach_section]
    if kb_content:
        sections.append(kb_content)
    if activities_summary:
        sections.append(activities_summary)

    combined = "\n\n---\n\n".join(sections)
    return [{"type": "text", "text": combined, "cache_control": {"type": "ephemeral"}}]


def load_strava_data() -> tuple[str, list[dict], dict]:
    """Returns (summary_string, activities_list, athlete_dict)."""
    client_id = os.getenv("STRAVA_CLIENT_ID", "").strip()
    client_secret = os.getenv("STRAVA_CLIENT_SECRET", "").strip()
    tokens = get_valid_token(client_id, client_secret)
    strava = StravaClient(tokens["access_token"])
    athlete = strava.get_athlete()
    activities = strava.get_activities(days=30)
    summary = strava.format_activities_summary(activities, athlete=athlete)
    return summary, activities, athlete


# ── Stats embed builder ────────────────────────────────────────────────────────

def build_stats_embed(activities: list[dict], athlete: dict) -> discord.Embed:
    """Build a rich Discord embed with key training metrics."""
    name = f"{athlete.get('firstname', '')} {athlete.get('lastname', '')}".strip()

    embed = discord.Embed(
        title="📊 Training Summary — Last 30 Days",
        description=f"Athlete: **{name}**" if name else "",
        color=0xFC4C02,  # Strava orange
    )

    if not activities:
        embed.description = "No activities in the last 30 days."
        return embed

    # Aggregate per sport
    sport_totals: dict[str, dict] = {}
    total_hr_sum = 0
    total_hr_count = 0

    for act in activities:
        sport = act.get("sport_type") or act.get("type", "Unknown")
        if sport not in sport_totals:
            sport_totals[sport] = {"count": 0, "distance_m": 0.0, "time_s": 0, "pace_sum": 0.0, "pace_count": 0}

        sport_totals[sport]["count"] += 1
        sport_totals[sport]["distance_m"] += act.get("distance", 0)
        sport_totals[sport]["time_s"] += act.get("moving_time") or act.get("elapsed_time", 0)

        dist_m = act.get("distance", 0)
        moving = act.get("moving_time", 0)
        if dist_m and moving and sport in {"Run", "TrailRun", "VirtualRun"}:
            sec_per_km = moving / (dist_m / 1000)
            sport_totals[sport]["pace_sum"] += sec_per_km
            sport_totals[sport]["pace_count"] += 1

        avg_hr = act.get("average_heartrate")
        if avg_hr:
            total_hr_sum += avg_hr
            total_hr_count += 1

    # Sport emojis
    sport_emoji = {
        "Run": "🏃", "TrailRun": "🏔️", "VirtualRun": "🏃",
        "Ride": "🚴", "VirtualRide": "🚴", "MountainBikeRide": "🚵",
        "Swim": "🏊", "Walk": "🚶", "Hike": "🥾",
    }

    for sport, totals in sorted(sport_totals.items()):
        emoji = sport_emoji.get(sport, "🏅")
        dist_km = totals["distance_m"] / 1000
        hrs = totals["time_s"] // 3600
        mins = (totals["time_s"] % 3600) // 60
        time_str = f"{hrs}h {mins:02d}m" if hrs else f"{mins}m"
        dist_str = f"{dist_km:.1f} km" if dist_km else "—"

        value_lines = [
            f"**Sessions:** {totals['count']}",
            f"**Distance:** {dist_str}",
            f"**Time:** {time_str}",
        ]

        if totals["pace_count"] > 0:
            avg_sec = totals["pace_sum"] / totals["pace_count"]
            value_lines.append(f"**Avg Pace:** {int(avg_sec // 60)}:{int(avg_sec % 60):02d}/km")

        embed.add_field(
            name=f"{emoji} {sport}",
            value="\n".join(value_lines),
            inline=True,
        )

    # Overall stats row
    if total_hr_count > 0:
        avg_hr = total_hr_sum / total_hr_count
        embed.add_field(name="❤️ Avg Heart Rate", value=f"{int(avg_hr)} bpm", inline=True)

    embed.add_field(name="📅 Total Activities", value=str(len(activities)), inline=True)
    embed.set_footer(text="Use /refresh to update • Ask me anything by @mentioning me")

    return embed


def build_weekly_embed(activities: list[dict]) -> discord.Embed:
    """Compare this week vs last week."""
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    this_week_start = now - timedelta(days=now.weekday() + 1)  # last Monday
    last_week_start = this_week_start - timedelta(days=7)

    this_week = [a for a in activities if datetime.fromisoformat(a["start_date"].replace("Z", "+00:00")) >= this_week_start]
    last_week = [a for a in activities if last_week_start <= datetime.fromisoformat(a["start_date"].replace("Z", "+00:00")) < this_week_start]

    def week_stats(acts):
        dist = sum(a.get("distance", 0) for a in acts) / 1000
        time_s = sum(a.get("moving_time") or a.get("elapsed_time", 0) for a in acts)
        hrs = time_s // 3600
        mins = (time_s % 3600) // 60
        return len(acts), dist, f"{hrs}h {mins:02d}m" if hrs else f"{mins}m"

    tw_count, tw_dist, tw_time = week_stats(this_week)
    lw_count, lw_dist, lw_time = week_stats(last_week)

    dist_diff = tw_dist - lw_dist
    diff_str = f"+{dist_diff:.1f} km" if dist_diff >= 0 else f"{dist_diff:.1f} km"
    diff_emoji = "📈" if dist_diff >= 0 else "📉"

    embed = discord.Embed(title="📅 Weekly Comparison", color=0xFC4C02)
    embed.add_field(
        name="This Week",
        value=f"**{tw_count}** sessions\n**{tw_dist:.1f} km**\n{tw_time}",
        inline=True,
    )
    embed.add_field(
        name="Last Week",
        value=f"**{lw_count}** sessions\n**{lw_dist:.1f} km**\n{lw_time}",
        inline=True,
    )
    embed.add_field(
        name=f"{diff_emoji} Volume Change",
        value=diff_str,
        inline=True,
    )
    embed.set_footer(text="Use /refresh to update data")
    return embed


# ── Bot setup ──────────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True

bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)


@bot.event
async def on_ready():
    global system_prompt, cached_activities, cached_athlete
    print(f"✅ Logged in as {bot.user}")
    await tree.sync()
    print("🔄 Loading Strava data...")
    try:
        summary, cached_activities, cached_athlete = load_strava_data()
        kb_content = load_knowledge_base()
        system_prompt = build_system_prompt(kb_content, summary)
        print(f"✅ Loaded {len(cached_activities)} activities. Bot ready!")
    except Exception as e:
        print(f"⚠️  Could not load Strava data: {e}")


# ── Chat (mention or DM) ───────────────────────────────────────────────────────

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    is_dm = isinstance(message.channel, discord.DMChannel)
    is_mentioned = bot.user in message.mentions

    if not is_dm and not is_mentioned:
        return

    content = message.content.replace(f"<@{bot.user.id}>", "").strip()
    if not content:
        await message.reply("Hey! Mention me with a question about your training 🏃")
        return

    channel_id = message.channel.id
    conversation_history[channel_id].append({"role": "user", "content": content})

    # Keep history bounded
    if len(conversation_history[channel_id]) > MAX_HISTORY:
        conversation_history[channel_id] = conversation_history[channel_id][-MAX_HISTORY:]

    async with message.channel.typing():
        try:
            response = claude_client.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=system_prompt,
                messages=conversation_history[channel_id],
            )
            assistant_text = "".join(b.text for b in response.content if b.type == "text")
            conversation_history[channel_id].append({"role": "assistant", "content": assistant_text})

            # Discord 2000 char limit — split if needed
            chunks = [assistant_text[i:i+1900] for i in range(0, len(assistant_text), 1900)]
            for chunk in chunks:
                await message.reply(chunk)

        except Exception as e:
            await message.reply(f"❌ Something went wrong: {e}")


# ── Slash commands ─────────────────────────────────────────────────────────────

@tree.command(name="stats", description="Show your training summary for the last 30 days")
async def stats_command(interaction: discord.Interaction):
    if not cached_activities:
        await interaction.response.send_message("⚠️ No data loaded yet. Try `/refresh` first.")
        return
    embed = build_stats_embed(cached_activities, cached_athlete)
    await interaction.response.send_message(embed=embed)


@tree.command(name="weekly", description="Compare this week vs last week")
async def weekly_command(interaction: discord.Interaction):
    if not cached_activities:
        await interaction.response.send_message("⚠️ No data loaded yet. Try `/refresh` first.")
        return
    embed = build_weekly_embed(cached_activities)
    await interaction.response.send_message(embed=embed)


@tree.command(name="refresh", description="Reload your latest Strava data")
async def refresh_command(interaction: discord.Interaction):
    global system_prompt, cached_activities, cached_athlete
    await interaction.response.defer()
    try:
        summary, cached_activities, cached_athlete = load_strava_data()
        kb_content = load_knowledge_base()
        system_prompt = build_system_prompt(kb_content, summary)
        await interaction.followup.send(f"✅ Refreshed! Loaded **{len(cached_activities)}** activities from the last 30 days.")
    except Exception as e:
        await interaction.followup.send(f"❌ Refresh failed: {e}")


@tree.command(name="clear", description="Clear conversation history in this channel")
async def clear_command(interaction: discord.Interaction):
    conversation_history[interaction.channel_id].clear()
    await interaction.response.send_message("✅ Conversation history cleared!")


@tree.command(name="status", description="Show bot status")
async def status_command(interaction: discord.Interaction):
    history_len = len(conversation_history.get(interaction.channel_id, []))
    embed = discord.Embed(title="🤖 Coach Status", color=0xFC4C02)
    embed.add_field(name="Model", value=MODEL, inline=True)
    embed.add_field(name="Activities loaded", value=str(len(cached_activities)), inline=True)
    embed.add_field(name="Messages in this channel", value=str(history_len), inline=True)
    embed.add_field(
        name="Commands",
        value="`/stats` · `/weekly` · `/refresh` · `/clear`\nOr just @mention me to chat!",
        inline=False,
    )
    await interaction.response.send_message(embed=embed)


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    required = {
        "DISCORD_BOT_TOKEN": os.getenv("DISCORD_BOT_TOKEN", "").strip(),
        "STRAVA_CLIENT_ID": os.getenv("STRAVA_CLIENT_ID", "").strip(),
        "STRAVA_CLIENT_SECRET": os.getenv("STRAVA_CLIENT_SECRET", "").strip(),
        "ANTHROPIC_API_KEY": os.getenv("ANTHROPIC_API_KEY", "").strip(),
    }
    missing = [k for k, v in required.items() if not v]
    if missing:
        print("Error: Missing required environment variables:")
        for var in missing:
            print(f"  - {var}")
        sys.exit(1)

    bot.run(required["DISCORD_BOT_TOKEN"])
