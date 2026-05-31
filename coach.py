#!/usr/bin/env python3
"""Strava Training Coach — Discord bot powered by Claude + Strava data."""

import asyncio
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

import aiohttp
from aiohttp import web
import anthropic
import discord
from discord.ext import commands
from dotenv import load_dotenv

from strava.auth import get_valid_token
from strava.client import StravaClient
from post_run import post_run_analysis

load_dotenv()

MODEL = "claude-haiku-4-5"
KB_DIR = Path("knowledge_base")
ABOUT_ME_FILE = KB_DIR / "about_me.md"
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
    {
        "name": "propose_profile_update",
        "description": (
            "Propose updating a field in the athlete's profile (about_me.md). "
            "Call this when the user mentions updated personal data: weight, HR zones, "
            "training paces, injuries, equipment, etc. "
            "The athlete will be shown a confirmation button before anything is saved. "
            "Only propose one field at a time."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "field": {
                    "type": "string",
                    "description": "Field name as in about_me.md, e.g. 'Weight', 'Max HR', 'Easy pace (Zone 2)'",
                },
                "value": {
                    "type": "string",
                    "description": "New value to set, e.g. '73 kg', '187', '5:45/km'",
                },
                "reason": {
                    "type": "string",
                    "description": "One-line reason for the proposed update",
                },
            },
            "required": ["field", "value", "reason"],
        },
    },
]


# ── Profile helpers ─────────────────────────────────────────────────────────────

def get_profile_field(field):
    """Read current value of a **Field:** line from about_me.md."""
    if not ABOUT_ME_FILE.exists():
        return None
    # Match value on same line only (no newline crossing)
    pattern = re.compile(r'\*\*' + re.escape(field) + r':\*\*[^\S\n]*([^\n]*)')
    for line in ABOUT_ME_FILE.read_text(encoding="utf-8").splitlines():
        m = pattern.search(line)
        if m:
            val = m.group(1).strip()
            return val if val else None
    return None


def update_profile_field(field, value):
    """Update or append a **Field:** value in about_me.md."""
    KB_DIR.mkdir(exist_ok=True)
    if not ABOUT_ME_FILE.exists():
        ABOUT_ME_FILE.write_text(
            "# About Me\n\n- **{}:** {}\n".format(field, value), encoding="utf-8"
        )
        return
    text = ABOUT_ME_FILE.read_text(encoding="utf-8")
    # Match **Field:** + rest of line (no newline crossing), replace whole thing
    pattern = r'\*\*' + re.escape(field) + r':\*\*[^\n]*'
    replacement = '**{}:** {}'.format(field, value)
    new_text, n = re.subn(pattern, replacement, text)
    if n == 0:
        new_text = text.rstrip() + "\n- **{}:** {}\n".format(field, value)
    ABOUT_ME_FILE.write_text(new_text, encoding="utf-8")


def extract_strava_profile_updates(athlete, activities):
    """Return {field: (old_value, new_value)} for fields that differ from about_me.md."""
    candidates = {}
    if athlete.get("weight"):
        candidates["Weight"] = "{:.1f} kg".format(athlete["weight"])
    max_hrs = [a["max_heartrate"] for a in activities if a.get("max_heartrate")]
    if max_hrs:
        candidates["Max HR"] = str(int(max(max_hrs)))
    updates = {}
    for field, new_val in candidates.items():
        current = get_profile_field(field)
        if current != new_val:
            updates[field] = (current, new_val)
    return updates


# ── Goal helpers & KB ───────────────────────────────────────────────────────────

def _category_slug(category_name):
    slug = category_name.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "_", slug)
    return slug.strip("_")


def _goals_file(category_name):
    return KB_DIR / "goals_{}.md".format(_category_slug(category_name))


def load_goals(category_name):
    """Return full goals file content for a category (rich markdown)."""
    f = _goals_file(category_name)
    if not f.exists():
        return ""
    return f.read_text(encoding="utf-8").strip()


def save_goals(category_name, goals):
    """Update 'Active Targets' section in goals file, preserving all other content."""
    KB_DIR.mkdir(exist_ok=True)
    f = _goals_file(category_name)
    targets_section = "## Active Targets\n\n" + "\n".join("- {}".format(g) for g in goals)

    if not f.exists():
        f.write_text("# Goals: {}\n\n{}\n".format(category_name, targets_section), encoding="utf-8")
        return

    text = f.read_text(encoding="utf-8")
    if "## Active Targets" in text:
        new_text = re.sub(
            r'## Active Targets\n.*?(?=\n## |\Z)',
            targets_section,
            text,
            flags=re.DOTALL,
        )
    else:
        new_text = text.rstrip() + "\n\n" + targets_section + "\n"
    f.write_text(new_text, encoding="utf-8")


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

## Profile management
You have a tool to propose profile updates (propose_profile_update). Use it when:
- The athlete mentions new personal data: weight, HR zones, training paces, injuries, equipment, etc.
- Only propose one field at a time — the athlete will confirm via button before it's saved
- After confirmation or rejection, continue the conversation naturally

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
    goals_content = load_goals(category_name) if category_name else ""
    return build_system_prompt(kb_content, activities_summary, goals_content)


def load_strava_data(client_id, client_secret):
    """Returns (summary_str, athlete_dict, activities_list)."""
    tokens = get_valid_token(client_id, client_secret)
    strava = StravaClient(tokens["access_token"])
    athlete = strava.get_athlete()
    activities = strava.get_activities(days=30)
    name = "{} {}".format(athlete.get("firstname", ""), athlete.get("lastname", "")).strip()
    print("Athlete: {}".format(name) if name else "Athlete loaded")
    print("Loaded {} activities from the last 30 days.".format(len(activities)))
    summary = strava.format_activities_summary(activities, athlete=athlete)
    return summary, athlete, activities


# ── Discord UI ──────────────────────────────────────────────────────────────────

class ProfileUpdateView(discord.ui.View):
    """Button view for confirming a profile field update."""

    def __init__(self, field, value, on_confirm, on_cancel):
        super().__init__(timeout=60)
        self.field = field
        self.value = value
        self._on_confirm = on_confirm
        self._on_cancel = on_cancel

    @discord.ui.button(label="✅ Simpan", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        self._on_confirm()
        await interaction.response.edit_message(
            content="✅ Profile updated: **{}** → **{}**".format(self.field, self.value),
            view=None,
        )

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        self._on_cancel()
        await interaction.response.edit_message(content="❌ Dibatalkan.", view=None)

    async def on_timeout(self):
        self._on_cancel()


# ── Strava Webhook registration ────────────────────────────────────────────────

STRAVA_PUSH_SUBS_URL = "https://www.strava.com/api/v3/push_subscriptions"


async def ensure_webhook_registered(client_id, client_secret, public_url, verify_token):
    """Register (or re-register) the Strava webhook subscription."""
    callback_url = public_url.rstrip("/") + "/webhook"

    async with aiohttp.ClientSession() as session:
        # Check existing subscription
        async with session.get(
            STRAVA_PUSH_SUBS_URL,
            params={"client_id": client_id, "client_secret": client_secret},
        ) as r:
            existing = await r.json() if r.status == 200 else []

        if existing:
            if existing[0].get("callback_url") == callback_url:
                print("Webhook already registered: {}".format(callback_url))
                return
            # Different URL — delete old subscription first
            sub_id = existing[0]["id"]
            async with session.delete(
                "{}/{}".format(STRAVA_PUSH_SUBS_URL, sub_id),
                params={"client_id": client_id, "client_secret": client_secret},
            ) as r:
                print("Deleted old webhook subscription (id={})".format(sub_id))

        # Register new subscription
        async with session.post(
            STRAVA_PUSH_SUBS_URL,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "callback_url": callback_url,
                "verify_token": verify_token,
            },
        ) as r:
            if r.status == 201:
                print("Webhook registered: {}".format(callback_url))
            else:
                body = await r.text()
                print("Webhook registration failed ({}) — {}".format(r.status, body))


# ── Discord bot ────────────────────────────────────────────────────────────────

async def run_bot(discord_token, client_id, client_secret, anthropic_key,
                  public_url=None, verify_token=None, port=8080):
    claude = anthropic.Anthropic(api_key=anthropic_key)

    print("\n=== Strava Training Coach (Discord) ===")
    print("Loading Strava data...")
    activities_summary, athlete, activities = load_strava_data(client_id, client_secret)
    kb_content = load_knowledge_base()
    if not kb_content:
        print("Tip: Edit knowledge_base/about_me.md to give your coach personal context.")

    state = {
        "activities_summary": activities_summary,
        "kb_content": kb_content,
        "athlete": athlete,
        "activities": activities,
    }

    # Per-channel conversation history: {channel_id: [{"role": ..., "content": ...}]}
    history = defaultdict(list)

    intents = discord.Intents.default()
    intents.message_content = True
    bot = commands.Bot(command_prefix="!", intents=intents)

    async def apply_strava_profile_updates(athlete, activities):
        """Auto-update about_me.md from Strava data and notify #feed if anything changed."""
        updates = extract_strava_profile_updates(athlete, activities)
        if not updates:
            return
        for field, (_, new_val) in updates.items():
            update_profile_field(field, new_val)
        state["kb_content"] = load_knowledge_base()
        feed_channel = discord.utils.find(
            lambda c: isinstance(c, discord.TextChannel) and c.name == "feed",
            bot.get_all_channels(),
        )
        if feed_channel:
            lines = [
                "- **{}**: {} → **{}**".format(f, old or "—", new)
                for f, (old, new) in updates.items()
            ]
            await feed_channel.send(
                "📊 **Profile auto-updated dari Strava:**\n" + "\n".join(lines)
            )

    # ── Strava webhook handlers (closures over bot + state) ──────────────────────

    async def handle_webhook_verify(request):
        """GET /webhook — Strava challenge verification."""
        if request.query.get("hub.verify_token") == verify_token:
            return web.json_response({"hub.challenge": request.query.get("hub.challenge", "")})
        return web.Response(status=403, text="Forbidden")

    async def handle_webhook_event(request):
        """POST /webhook — incoming Strava event."""
        try:
            data = await request.json()
        except Exception:
            return web.Response(status=400, text="Bad JSON")
        # Respond immediately; process async
        asyncio.create_task(process_strava_event(data))
        return web.Response(status=200, text="OK")

    async def process_strava_event(data):
        obj_type    = data.get("object_type")
        aspect      = data.get("aspect_type")
        activity_id = data.get("object_id")

        if obj_type != "activity" or aspect not in ("create", "update"):
            return

        print("Strava event: {} {} (id={})".format(aspect, obj_type, activity_id))

        try:
            new_summary, new_athlete, new_activities = load_strava_data(client_id, client_secret)
            state["activities_summary"] = new_summary
            state["athlete"]            = new_athlete
            state["activities"]         = new_activities
            state["kb_content"]         = load_knowledge_base()
            await apply_strava_profile_updates(new_athlete, new_activities)
        except Exception as e:
            print("Error refreshing after webhook: {}".format(e))
            return

        post_run_channel_name = os.getenv("POST_RUN_CHANNEL", "feed")
        feed_channel = discord.utils.find(
            lambda c: isinstance(c, discord.TextChannel) and c.name == post_run_channel_name,
            bot.get_all_channels(),
        )

        if not feed_channel:
            print("Channel #{} not found — skipping post.".format(post_run_channel_name))
            return

        if aspect == "create" and activity_id:
            try:
                tokens = get_valid_token(client_id, client_secret)
                strava = StravaClient(tokens["access_token"])
                full_activity = strava.get_activity(activity_id)
                sport = full_activity.get("sport_type") or full_activity.get("type", "")

                RUN_SPORTS = {"Run", "TrailRun", "VirtualRun"}
                if sport in RUN_SPORTS:
                    # Load full goals file content (preserves structure)
                    goals_content = ""
                    for goals_file in sorted(KB_DIR.glob("goals_*.md")):
                        text = goals_file.read_text(encoding="utf-8").strip()
                        if text:
                            goals_content += text + "\n\n"
                    goals_content = goals_content.strip()

                    await post_run_analysis(
                        activity=full_activity,
                        activities=state["activities"],
                        athlete=state["athlete"],
                        kb_content=state["kb_content"],
                        goals_content=goals_content,
                        channel=feed_channel,
                        claude_client=claude,
                    )
                else:
                    await feed_channel.send("📊 **New activity synced** — {}".format(
                        full_activity.get("name", sport)
                    ))
            except Exception as e:
                print("Post-run analysis failed: {}".format(e))
                await feed_channel.send("📊 **New activity synced** — data refreshed.")
        else:
            await feed_channel.send("🔄 Activity updated — data refreshed.")

    # ── Start aiohttp webhook server ─────────────────────────────────────────────

    if public_url and verify_token:
        app = web.Application()
        app.router.add_get("/webhook", handle_webhook_verify)
        app.router.add_post("/webhook", handle_webhook_event)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", port).start()
        print("Webhook server listening on port {}".format(port))
        await ensure_webhook_registered(client_id, client_secret, public_url, verify_token)
    else:
        print("PUBLIC_URL or WEBHOOK_VERIFY_TOKEN not set — webhook disabled.")

    @bot.event
    async def on_ready():
        print("Bot online as {}".format(bot.user))
        print("Mention @{} to chat.".format(bot.user.name))
        await apply_strava_profile_updates(state["athlete"], state["activities"])

    @bot.command(name="refresh")
    async def refresh(ctx):
        """Reload Strava data and reset channel history."""
        async with ctx.typing():
            try:
                new_summary, new_athlete, new_activities = load_strava_data(client_id, client_secret)
                new_kb = load_knowledge_base()
                state["activities_summary"] = new_summary
                state["kb_content"] = new_kb
                state["athlete"] = new_athlete
                state["activities"] = new_activities
                history[ctx.channel.id].clear()
                await apply_strava_profile_updates(new_athlete, new_activities)

                # Show what was loaded so user can verify freshness
                count = len(new_activities)
                if new_activities:
                    from datetime import datetime, timezone, timedelta
                    WIB = timezone(timedelta(hours=7))
                    latest = max(new_activities, key=lambda a: a["start_date"])
                    latest_date = datetime.fromisoformat(
                        latest["start_date"].replace("Z", "+00:00")
                    ).astimezone(WIB).strftime("%d %b %Y, %H:%M WIB")
                    latest_name = latest.get("name", "Untitled")
                    await ctx.send(
                        "✅ Refreshed — **{} activities** loaded.\n"
                        "Aktivitas terbaru: **{}** ({})".format(count, latest_name, latest_date)
                    )
                else:
                    await ctx.send("✅ Refreshed — no activities found in the last 30 days.")
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
                                content = load_goals(category_name) if category_name else ""
                                result = content if content else "No goals set yet."

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

                            elif block.name == "propose_profile_update":
                                field = block.input["field"]
                                value = block.input["value"]

                                confirmed_evt = asyncio.Event()
                                cancelled_evt = asyncio.Event()

                                view = ProfileUpdateView(
                                    field, value,
                                    on_confirm=confirmed_evt.set,
                                    on_cancel=cancelled_evt.set,
                                )
                                prompt_msg = await msg.channel.send(
                                    "Update **{}** → **{}**?".format(field, value),
                                    view=view,
                                )

                                done, pending = await asyncio.wait(
                                    [
                                        asyncio.create_task(confirmed_evt.wait()),
                                        asyncio.create_task(cancelled_evt.wait()),
                                    ],
                                    timeout=60,
                                    return_when=asyncio.FIRST_COMPLETED,
                                )
                                for task in pending:
                                    task.cancel()

                                if confirmed_evt.is_set():
                                    update_profile_field(field, value)
                                    state["kb_content"] = load_knowledge_base()
                                    system_prompt = build_system_prompt_for_category(
                                        category_name, state["kb_content"], state["activities_summary"]
                                    )
                                    result = "Profile updated: {} = {}.".format(field, value)
                                else:
                                    if not done:  # timeout
                                        await prompt_msg.edit(
                                            content="⏱️ Timeout, dibatalkan.", view=None
                                        )
                                    result = "Update cancelled."

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

    await bot.start(discord_token)


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    expected = [
        "STRAVA_CLIENT_ID", "STRAVA_CLIENT_SECRET",
        "ANTHROPIC_API_KEY", "DISCORD_BOT_TOKEN", "STRAVA_REFRESH_TOKEN",
    ]
    for var in expected:
        print("ENV {}: {}".format(var, "SET" if os.getenv(var) else "MISSING"))

    client_id     = os.getenv("STRAVA_CLIENT_ID", "").strip()
    client_secret = os.getenv("STRAVA_CLIENT_SECRET", "").strip()
    anthropic_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    discord_token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
    public_url    = os.getenv("PUBLIC_URL", "").strip()
    verify_token  = os.getenv("WEBHOOK_VERIFY_TOKEN", "").strip()
    port          = int(os.getenv("PORT", "8080"))

    missing = []
    if not client_id:     missing.append("STRAVA_CLIENT_ID")
    if not client_secret: missing.append("STRAVA_CLIENT_SECRET")
    if not anthropic_key: missing.append("ANTHROPIC_API_KEY")
    if not discord_token: missing.append("DISCORD_BOT_TOKEN")

    if missing:
        print("Error: Missing required environment variables:")
        for var in missing:
            print("  - {}".format(var))
        print("\nAdd them to your .env file.")
        sys.exit(1)

    if not public_url or not verify_token:
        print("Warning: PUBLIC_URL or WEBHOOK_VERIFY_TOKEN not set — real-time Strava sync disabled.")

    asyncio.run(run_bot(
        discord_token, client_id, client_secret, anthropic_key,
        public_url=public_url or None,
        verify_token=verify_token or None,
        port=port,
    ))


if __name__ == "__main__":
    main()
