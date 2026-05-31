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
        lines += [
            "",
            "## Training load",
            "- This week: {} km ({} runs)".format(load.get("this_week_km"), load.get("this_week_runs")),
            "- Last week: {} km ({} runs)".format(load.get("last_week_km"), load.get("last_week_runs")),
        ]
        if load.get("change_pct") is not None:
            lines.append("- Week-over-week change: {}{}%".format(
                "+" if load["change_pct"] >= 0 else "", load["change_pct"]
            ))

    if milestones:
        lines += ["", "## Milestones", *["- {}".format(m) for m in milestones]]

    context_block = "\n".join(lines)

    prompt = (
        "You are a personal running coach. Write a short, sharp post-run insight "
        "in Bahasa Indonesia (2-4 sentences max). Be specific — reference the actual "
        "numbers, conditions, and patterns. Highlight what's most interesting or "
        "actionable. Don't be generic. Don't start with 'Bagus!' or 'Luar biasa!'.\n\n"
        "## Athlete profile & goals\n{}\n{}\n\n"
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


# ── Discord embed builder ───────────────────────────────────────────────────────

def build_embed(activity: dict, enriched: dict, insight: str) -> discord.Embed:
    sport = activity.get("sport_type") or activity.get("type", "Run")
    emoji = SPORT_EMOJI.get(sport, "🏅")
    dist_km = activity.get("distance", 0) / 1000
    moving = activity.get("moving_time", 0)
    avg_hr = activity.get("average_heartrate")
    elev = activity.get("total_elevation_gain")

    start_time = datetime.fromisoformat(
        activity["start_date"].replace("Z", "+00:00")
    ).astimezone(WIB)

    weather = enriched.get("weather") or {}
    aqi_data = enriched.get("aqi") or {}
    location_name = enriched.get("location_name") or ""
    time_context = enriched.get("time_context") or ""
    decoupling = enriched.get("aerobic_decoupling")
    hr_zones = enriched.get("hr_zones") or {}
    training_load = enriched.get("training_load") or {}
    milestones = enriched.get("milestones") or []
    hr_baseline = enriched.get("hr_baseline")
    pace_sec = enriched.get("pace_sec_km")
    adj_pace_sec = enriched.get("adjusted_pace_sec_km")

    aqi_val = aqi_data.get("aqi")
    embed = discord.Embed(
        title="{} {} — {:.1f} km".format(emoji, activity.get("name", sport), dist_km),
        color=_embed_color(aqi_val, decoupling),
    )

    subtitle_parts = []
    if location_name:
        subtitle_parts.append("📍 {}".format(location_name))
    if time_context:
        subtitle_parts.append("🌅 {}".format(time_context).replace("run", "").strip() if "sunrise" in time_context else "⏰ {}".format(time_context))
    subtitle_parts.append("🗓️ {}".format(start_time.strftime("%a %d %b")))
    embed.description = "  ·  ".join(subtitle_parts)

    # Conditions bar
    cond_parts = []
    if weather.get("temp_c") is not None:
        cond_parts.append("🌡️ {}°C / feels {}°C".format(
            round(weather["temp_c"]), round(weather["feels_like_c"])
        ))
    if weather.get("humidity_pct"):
        cond_parts.append("💧 {}%".format(weather["humidity_pct"]))
    if aqi_data:
        aqi_label = "🌿 AQI {} — {}".format(aqi_val, aqi_data.get("level", ""))
        cond_parts.append(aqi_label)
    if cond_parts:
        embed.add_field(name="Conditions", value="  ·  ".join(cond_parts), inline=False)

    # Core metrics
    metrics = []
    if pace_sec:
        pace_line = "**{}**/km".format(_fmt_pace(pace_sec))
        if adj_pace_sec and abs(adj_pace_sec - pace_sec) > 5:
            pace_line += "\n≈ **{}** adjusted".format(_fmt_pace(adj_pace_sec))
        metrics.append(("Pace", pace_line))

    metrics.append(("Duration", _fmt_duration(moving)))

    if avg_hr:
        hr_line = "**{}** bpm".format(int(avg_hr))
        delta = _hr_delta_str(avg_hr, hr_baseline)
        if delta:
            hr_line += "\n{}".format(delta)
        metrics.append(("Avg HR", hr_line))

    if elev and elev > 5:
        metrics.append(("Elev gain", "+{}m".format(int(elev))))

    for name, value in metrics:
        embed.add_field(name=name, value=value, inline=True)

    # Milestone banner
    if milestones:
        embed.add_field(
            name="🏆 Milestone",
            value="\n".join("• {}".format(m) for m in milestones),
            inline=False,
        )

    # HR zones + decoupling
    if hr_zones:
        zone_parts = []
        for zone, pct in hr_zones.items():
            short = zone.replace("Zone ", "Z")
            zone_parts.append("{} {}%".format(short, pct))
        zone_str = "  ·  ".join(zone_parts)
        if decoupling is not None:
            quality = "✅ solid" if decoupling < 5 else ("⚠️ moderate drift" if decoupling < 10 else "❌ high drift")
            zone_str += "\nAerobic decoupling **{}%** — {}".format(decoupling, quality)
        embed.add_field(name="Effort quality", value=zone_str, inline=False)

    # Goal alignment
    goal_lines = _build_goal_alignment(training_load, pace_sec, activity)
    if goal_lines:
        embed.add_field(name="Training load", value="\n".join(goal_lines), inline=False)

    # Coach insight
    if insight:
        embed.add_field(name="💬 Coach", value=insight, inline=False)

    # Footer
    strava_id = activity.get("id")
    footer_parts = []
    if training_load.get("change_pct") is not None:
        change = training_load["change_pct"]
        sign = "+" if change >= 0 else ""
        footer_parts.append("Weekly volume {}{}% vs last week".format(sign, change))
    if strava_id:
        footer_parts.append("strava.com/activities/{}".format(strava_id))
    embed.set_footer(text="  ·  ".join(footer_parts))

    return embed


def _build_goal_alignment(training_load: dict, pace_sec: float | None, activity: dict) -> list[str]:
    lines = []
    tw = training_load.get("this_week_km", 0)
    lw = training_load.get("last_week_km", 0)
    change = training_load.get("change_pct")

    if tw > 0:
        lines.append("📅 This week: **{} km** ({} runs)".format(
            tw, training_load.get("this_week_runs", 0)
        ))
    if change is not None:
        sign = "+" if change >= 0 else ""
        icon = "📈" if change >= 0 else "📉"
        lines.append("{} {}{}% vs last week ({} km)".format(icon, sign, change, lw))
        if change > 30:
            lines.append("⚠️ Volume spike >30% — consider an easy day")

    return lines


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

    prompt = _build_insight_prompt(activity, enriched, kb_content, goals_content)
    insight = _generate_insight(prompt, claude_client)

    embed = build_embed(activity, enriched, insight)

    try:
        await channel.send(embed=embed)
    except Exception as e:
        print("Failed to post run analysis: {}".format(e))
