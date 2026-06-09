"""Post-run analysis: build Discord embed + Claude insight and post to #feed."""

import os
from datetime import datetime, timedelta, timezone

import anthropic
import discord

from enrichment import enrich_activity

WIB = timezone(timedelta(hours=7))

RUN_SPORTS = {"Run", "TrailRun", "VirtualRun"}

MODEL = "claude-haiku-4-5"
MAX_TOKENS = 600

SPORT_EMOJI = {
    "Run": "🏃",
    "TrailRun": "🏔️",
    "VirtualRun": "🏃",
}


# ── Formatting helpers ──────────────────────────────────────────────────────────

def _fmt_pace(sec_km: float) -> str:
    m, s = divmod(int(sec_km), 60)
    return "{}:{:02d}".format(m, s)


def _fmt_duration(seconds: int) -> str:
    h = seconds // 3600
    m = (seconds % 3600) // 60
    if h:
        return "{}h {:02d}m".format(h, m)
    return "{}m {:02d}s".format(m, seconds % 60)


def _hr_delta_str(avg_hr: float | None, baseline: int | None) -> str:
    if not avg_hr or not baseline:
        return ""
    delta = int(avg_hr) - baseline
    sign = "+" if delta >= 0 else ""
    return "{}{} vs baseline".format(sign, delta)


def _zone_color(zone: str) -> int:
    return {
        "Zone 1": 0x5de08a,
        "Zone 2": 0x5de08a,
        "Zone 3": 0xe8a84a,
        "Zone 4": 0xe06060,
        "Zone 5": 0xe06060,
    }.get(zone, 0x949ba4)


def _aqi_color(aqi: int) -> int:
    if aqi <= 50:
        return 0x5de08a
    if aqi <= 100:
        return 0xe8c84a
    if aqi <= 150:
        return 0xe8a84a
    if aqi <= 200:
        return 0xe06060
    return 0x990000


def _embed_color(aqi_val: int | None, decoupling: float | None) -> int:
    if aqi_val and aqi_val > 150:
        return 0xe06060
    if decoupling and decoupling > 8:
        return 0xe8a84a
    return 0xFC4C02


# ── Claude insight generation ───────────────────────────────────────────────────

def _build_insight_prompt(activity: dict, enriched: dict, kb_content: str, goals_content: str) -> str:
    dist_km = activity.get("distance", 0) / 1000
    moving = activity.get("moving_time", 0)
    avg_hr = activity.get("average_heartrate")
    sport = activity.get("sport_type") or activity.get("type", "Run")

    pace_str = _fmt_pace(enriched["pace_sec_km"]) + "/km" if enriched.get("pace_sec_km") else "unknown"
    adj_pace_str = _fmt_pace(enriched["adjusted_pace_sec_km"]) + "/km" if enriched.get("adjusted_pace_sec_km") else None

    weather = enriched.get("weather") or {}
    aqi = enriched.get("aqi") or {}
    hr_zones = enriched.get("hr_zones") or {}
    decoupling = enriched.get("aerobic_decoupling")
    load = enriched.get("training_load") or {}
    milestones = enriched.get("milestones") or []
    hr_baseline = enriched.get("hr_baseline")

    lines = [
        "## Activity",
        "- Type: {}".format(sport),
        "- Distance: {:.2f} km".format(dist_km),
        "- Duration: {}".format(_fmt_duration(moving)),
        "- Pace: {}".format(pace_str),
    ]
    if adj_pace_str and adj_pace_str != pace_str:
        lines.append("- Heat-adjusted equivalent pace: {}".format(adj_pace_str))
    if avg_hr:
        lines.append("- Avg HR: {} bpm{}".format(
            int(avg_hr),
            " (baseline ~{} bpm, delta {}{})".format(
                hr_baseline,
                "+" if int(avg_hr) >= hr_baseline else "",
                int(avg_hr) - hr_baseline
            ) if hr_baseline else ""
        ))
    if activity.get("total_elevation_gain"):
        lines.append("- Elevation gain: +{}m".format(int(activity["total_elevation_gain"])))

    if weather:
        lines += [
            "",
            "## Conditions",
            "- Temperature: {}°C (feels like {}°C)".format(
                round(weather.get("temp_c", 0)), round(weather.get("feels_like_c", 0))
            ),
            "- Humidity: {}%".format(weather.get("humidity_pct", "")),
            "- Wind: {} km/h".format(weather.get("wind_kmh", "")),
        ]
    if aqi:
        lines.append("- AQI: {} ({})".format(aqi.get("aqi"), aqi.get("level", "")))

    if hr_zones:
        lines += ["", "## HR zones"]
        for zone, pct in hr_zones.items():
            lines.append("- {}: {}%".format(zone, pct))

    if decoupling is not None:
        lines += ["", "## Aerobic decoupling: {}%".format(decoupling)]

    if load:
        days_into = load.get("days_into_week", 7)
        lines += [
            "",
            "## Training load",
            "- This week: {} km ({} runs)".format(load.get("this_week_km"), load.get("this_week_runs")),
            "- Last week: {} km ({} runs)".format(load.get("last_week_km"), load.get("last_week_runs")),
        ]
        if days_into <= 2:
            lines.append(
                "- Week is only {} day(s) old — do NOT compare or comment on weekly volume trend.".format(days_into)
            )
        elif load.get("change_pct") is not None:
            lines.append("- Week-over-week change: {}{}%".format(
                "+" if load["change_pct"] >= 0 else "", load["change_pct"]
            ))

    if milestones:
        lines += ["", "## Milestones", *["- {}".format(m) for m in milestones]]

    context_block = "\n".join(lines)

    prompt = (
        "You are a personal running coach. Write a short, sharp post-run insight "
        "in Bahasa Indonesia (2-4 sentences max). Be specific — reference actual "
        "numbers. Don't be generic. Don't start with 'Bagus!' or 'Luar biasa!'.\n\n"
        "## Athlete Profile\n{}\n\n"
        "## Training Goals\n{}\n\n"
        "Evaluate this run against the athlete's Training Goals above: "
        "flag if the effort level, HR zone distribution, or pace is misaligned with their targets. "
        "Highlight what's most actionable for their next session.\n\n"
        "{}"
    ).format(kb_content or "(no profile)", goals_content or "(no goals)", context_block)

    return prompt


def _generate_insight(prompt: str, claude_client: anthropic.Anthropic) -> str:
    try:
        response = claude_client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(b.text for b in response.content if b.type == "text").strip()
    except Exception as e:
        print("Claude insight failed: {}".format(e))
        return ""


def _generate_goal_alignment(
    activity: dict,
    enriched: dict,
    goals_content: str,
    claude_client: anthropic.Anthropic,
) -> list[dict]:
    """Ask Claude to evaluate this run against the athlete's goals.

    Returns a list of {"status": "ok"|"warning"|"flag", "text": "..."} dicts.
    Returns [] on failure or if no goals.
    """
    if not goals_content or not goals_content.strip():
        return []

    import json as _json

    dist_km = activity.get("distance", 0) / 1000
    moving = activity.get("moving_time", 0)
    avg_hr = activity.get("average_heartrate")
    hr_zones = enriched.get("hr_zones") or {}
    training_load = enriched.get("training_load") or {}
    pace_sec = enriched.get("pace_sec_km")

    days_into = training_load.get("days_into_week", 7)
    activity_summary = [
        "- Distance: {:.2f} km".format(dist_km),
        "- Duration: {}".format(_fmt_duration(moving)),
        "- Pace: {}".format(_fmt_pace(pace_sec) + "/km" if pace_sec else "unknown"),
        "- Avg HR: {} bpm".format(int(avg_hr)) if avg_hr else "",
        "- HR zones: {}".format(", ".join("{} {}%".format(z, p) for z, p in hr_zones.items())) if hr_zones else "",
        "- This week so far: {} km ({} runs) — day {} of the week".format(
            training_load.get("this_week_km", 0),
            training_load.get("this_week_runs", 0),
            days_into,
        ),
    ]
    activity_summary = "\n".join(l for l in activity_summary if l)

    weekly_volume_instruction = (
        "SKIP weekly volume/frequency check — it is only day {} of the week, "
        "not enough data to judge weekly load yet.".format(days_into)
        if days_into <= 2
        else "weekly volume/frequency"
    )

    prompt = (
        "Given these training goals and this single run's data, return a JSON array "
        "of 3-4 goal alignment checks. Each item must have:\n"
        '- "status": "ok", "warning", or "flag"\n'
        '- "text": one concise line in Bahasa Indonesia with specific numbers\n\n'
        "Focus on: easy run HR zone ratio, pace vs goal pace, {}, "
        "intensity balance. Be specific with numbers. Return ONLY valid JSON, no prose.\n\n"
        "## Training Goals\n{}\n\n"
        "## This Run\n{}"
    ).format(weekly_volume_instruction, goals_content, activity_summary)

    try:
        response = claude_client.messages.create(
            model=MODEL,
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = "".join(b.text for b in response.content if b.type == "text").strip()
        # Strip markdown code fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        checks = _json.loads(raw)
        if isinstance(checks, list):
            return checks[:4]
    except Exception as e:
        print("Goal alignment generation failed: {}".format(e))
    return []


# ── Recovery estimate ───────────────────────────────────────────────────────────

def _estimate_recovery_hours(hr_zones: dict, decoupling: float | None, moving_secs: int) -> str:
    """Estimate recovery time based on zone distribution and duration."""
    z4_plus = hr_zones.get("Zone 4", 0) + hr_zones.get("Zone 5", 0)
    z3 = hr_zones.get("Zone 3", 0)
    hours_run = moving_secs / 3600

    if z4_plus > 20:
        base = 48
    elif z3 > 50:
        base = 36
    else:
        base = 24

    if hours_run > 1.5:
        base += 8
    elif hours_run > 1.0:
        base += 4

    if decoupling and decoupling > 7:
        base += 8

    low = max(12, base - 4)
    high = base + 4
    return "~{}–{} jam".format(low, high)


# ── HR zone display helpers ──────────────────────────────────────────────────────

def _fmt_zones(hr_zones: dict) -> str:
    """Format zone distribution as 'Zone 2 — 61%  ·  Zone 3 — 30%  ·  Zone 4+ — 9%'."""
    merged: dict[str, int] = {}
    for zone, pct in hr_zones.items():
        if zone in ("Zone 4", "Zone 5"):
            merged["Zone 4+"] = merged.get("Zone 4+", 0) + pct
        else:
            merged[zone] = pct

    order = ["Zone 1", "Zone 2", "Zone 3", "Zone 4+"]
    parts = []
    for z in order:
        if z in merged and merged[z] > 0:
            parts.append("**{}** — {}%".format(z, merged[z]))
    return "  ·  ".join(parts)


# ── Discord embed builder ───────────────────────────────────────────────────────

def build_embed(activity: dict, enriched: dict, insight: str, goal_checks: list | None = None) -> discord.Embed:
    sport      = activity.get("sport_type") or activity.get("type", "Run")
    emoji      = SPORT_EMOJI.get(sport, "🏅")
    dist_km    = activity.get("distance", 0) / 1000
    moving     = activity.get("moving_time", 0)
    avg_hr     = activity.get("average_heartrate")
    elev       = activity.get("total_elevation_gain")

    start_time = datetime.fromisoformat(
        activity["start_date"].replace("Z", "+00:00")
    ).astimezone(WIB)

    weather        = enriched.get("weather") or {}
    aqi_data       = enriched.get("aqi") or {}
    location_name  = enriched.get("location_name") or ""
    time_context   = enriched.get("time_context") or ""
    decoupling     = enriched.get("aerobic_decoupling")
    hr_zones       = enriched.get("hr_zones") or {}
    training_load  = enriched.get("training_load") or {}
    milestones     = enriched.get("milestones") or []
    hr_baseline    = enriched.get("hr_baseline")
    pace_sec       = enriched.get("pace_sec_km")
    adj_pace_sec   = enriched.get("adjusted_pace_sec_km")
    aqi_val        = aqi_data.get("aqi")

    embed = discord.Embed(
        title="{} {} — {:.1f} km".format(emoji, activity.get("name", sport), dist_km),
        color=_embed_color(aqi_val, decoupling),
    )

    # ── Subtitle ──────────────────────────────────────────────────────────────
    subtitle_parts = []
    if location_name:
        subtitle_parts.append("📍 {}".format(location_name))
    if time_context:
        tc = time_context.replace(" run", "").replace(" Run", "")
        subtitle_parts.append("🌅 {}".format(tc) if "sunrise" in time_context.lower() else "⏰ {}".format(tc))
    subtitle_parts.append(start_time.strftime("%a %d %b"))
    embed.description = "  ·  ".join(subtitle_parts)

    # ── Conditions ────────────────────────────────────────────────────────────
    if weather or aqi_data:
        cond_lines = []
        if weather.get("temp_c") is not None:
            weather_str = "🌡️ **{}°C** / feels {}°C".format(
                round(weather["temp_c"]), round(weather["feels_like_c"])
            )
            if weather.get("humidity_pct"):
                weather_str += "   💧 {}% humidity".format(weather["humidity_pct"])
            cond_lines.append(weather_str)
        if aqi_data and aqi_val is not None:
            aqi_icon = "🟢" if aqi_val <= 50 else ("🟡" if aqi_val <= 100 else ("🟠" if aqi_val <= 150 else "🔴"))
            cond_lines.append("{} AQI {} — {}".format(aqi_icon, aqi_val, aqi_data.get("level", "")))
        embed.add_field(name="Conditions", value="\n".join(cond_lines), inline=False)

    # ── Core metrics (inline) ─────────────────────────────────────────────────
    if pace_sec:
        pace_val = "**{}**/km".format(_fmt_pace(pace_sec))
        if adj_pace_sec and abs(adj_pace_sec - pace_sec) > 5:
            pace_val += "\n≈ **{}** adjusted".format(_fmt_pace(adj_pace_sec))
        embed.add_field(name="Pace", value=pace_val, inline=True)

    embed.add_field(name="Duration", value="**{}**\nmoving".format(_fmt_duration(moving)), inline=True)

    if avg_hr:
        hr_val = "**{}** bpm".format(int(avg_hr))
        if hr_baseline:
            delta = int(avg_hr) - hr_baseline
            hr_val += "\n{}{} vs baseline".format("+" if delta >= 0 else "", delta)
        embed.add_field(name="Avg HR", value=hr_val, inline=True)

    if elev and elev > 5:
        embed.add_field(name="Elev gain", value="**+{}m**\ntotal".format(int(elev)), inline=True)

    # ── Milestone ─────────────────────────────────────────────────────────────
    if milestones:
        embed.add_field(
            name="🏆 Milestone",
            value="\n".join("• {}".format(m) for m in milestones),
            inline=False,
        )

    # ── Effort quality ────────────────────────────────────────────────────────
    if hr_zones:
        effort_lines = [_fmt_zones(hr_zones)]
        if decoupling is not None:
            if decoupling < 5:
                dc_label = "bagus untuk long run"
            elif decoupling < 10:
                dc_label = "moderate drift"
            else:
                dc_label = "high drift — perlu perhatian"
            effort_lines.append("Aerobic decoupling **{}%** — {}".format(decoupling, dc_label))
        embed.add_field(name="Effort quality", value="\n".join(effort_lines), inline=False)

    # ── Goal alignment ────────────────────────────────────────────────────────
    if goal_checks:
        icon_map = {"ok": "✅", "warning": "⚠️", "flag": "❌"}
        check_lines = [
            "{} {}".format(icon_map.get(c.get("status", "ok"), "•"), c.get("text", ""))
            for c in goal_checks
        ]
        embed.add_field(name="Goal alignment", value="\n".join(check_lines), inline=False)

    # ── Coach insight ─────────────────────────────────────────────────────────
    if insight:
        embed.add_field(name="Coach insight", value=insight, inline=False)

    # ── Footer: recovery + load spike + strava ────────────────────────────────
    footer_parts = []
    if hr_zones and moving:
        recovery_str = _estimate_recovery_hours(hr_zones, decoupling, moving)
        footer_parts.append("Est. recovery {}".format(recovery_str))
    if training_load.get("change_pct") is not None and training_load.get("days_into_week", 7) > 2:
        change = training_load["change_pct"]
        sign = "+" if change >= 0 else ""
        footer_parts.append("Load spike {}{}% vs minggu lalu".format(sign, change))
    strava_id = activity.get("id")
    if strava_id:
        footer_parts.append("strava.com/activities/{}".format(strava_id))
    embed.set_footer(text="  ·  ".join(footer_parts))

    return embed


# ── Main entry point ────────────────────────────────────────────────────────────

async def post_run_analysis(
    activity: dict,
    activities: list[dict],
    athlete: dict,
    kb_content: str,
    goals_content: str,
    channel: discord.TextChannel,
    claude_client: anthropic.Anthropic,
) -> None:
    """Build and post the full enriched run analysis to a Discord channel."""

    sport = activity.get("sport_type") or activity.get("type", "")
    if sport not in RUN_SPORTS:
        return

    enriched = await enrich_activity(activity, activities)

    goal_checks = _generate_goal_alignment(activity, enriched, goals_content, claude_client)

    prompt = _build_insight_prompt(activity, enriched, kb_content, goals_content)
    insight = _generate_insight(prompt, claude_client)

    embed = build_embed(activity, enriched, insight, goal_checks=goal_checks)

    try:
        await channel.send(embed=embed)
    except Exception as e:
        print("Failed to post run analysis: {}".format(e))
