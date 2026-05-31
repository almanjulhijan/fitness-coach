"""Weekly training analysis — aggregates run + gym data, posts to Discord."""

import asyncio
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import anthropic
import discord

from enrichment import (
    _read_max_hr_from_kb,
    compute_aerobic_decoupling,
    compute_hr_zones,
    get_time_context,
    get_weather,
    heat_adjusted_pace,
)
from strava.client import StravaClient

WIB = timezone(timedelta(hours=7))
RUN_SPORTS = {"Run", "TrailRun", "VirtualRun"}
GYM_SPORTS = {"WeightTraining", "Workout", "Crossfit"}

MODEL = "claude-haiku-4-5"
MAX_TOKENS = 500

DAYS_SHORT = ["SEN", "SEL", "RAB", "KAM", "JUM", "SAB", "MIN"]
TYPE_LABEL = {"upper": "UB", "lower": "LB", "full": "FB", "skill": "SK", "unknown": "??"}

LOWER_KEYWORDS = {
    "squat", "deadlift", "lunge", "leg press", "leg curl", "leg extension",
    "calf", "hip thrust", "glute", "romanian", "bulgarian", "box jump",
    "step up", "pistol", "nordic", "hamstring", "quad", "rdl",
}
UPPER_KEYWORDS = {
    "pull up", "pull-up", "push up", "push-up", "chin up", "bench", "row",
    "overhead press", "dip", "curl", "tricep", "lat", "shoulder press",
    "chest", "muscle up", "muscle-up", "handstand", "face pull", "fly",
    "incline", "decline", "bicep",
}
SKILL_KEYWORDS = {
    "muscle up", "muscle-up", "handstand", "planche", "front lever",
    "back lever", "l-sit", "human flag", "ring muscle",
}


# ── Muscle group classification ────────────────────────────────────────────────

def classify_muscle_group(description: str) -> dict:
    """Parse Hevy description → classify muscle group and extract volume."""
    if not description:
        return {"type": "unknown", "exercises": [], "cns_heavy": False, "sets": 0, "reps": 0}

    exercises = []
    total_sets = 0
    total_reps = 0

    for line in description.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        ll = line.lower()
        if ll.startswith("logged with"):
            continue
        if ll.startswith("set ") and ":" in ll:
            total_sets += 1
            m = re.search(r"(\d+)\s*reps?", ll)
            if m:
                total_reps += int(m.group(1))
            continue
        if len(line) >= 3:
            exercises.append(line)

    lower = sum(1 for e in exercises if any(k in e.lower() for k in LOWER_KEYWORDS))
    upper = sum(1 for e in exercises if any(k in e.lower() for k in UPPER_KEYWORDS))
    skill = sum(1 for e in exercises if any(k in e.lower() for k in SKILL_KEYWORDS))

    if skill > 0 and lower == 0:
        group_type = "skill"
    elif lower > 0 and upper == 0:
        group_type = "lower"
    elif upper > 0 and lower == 0:
        group_type = "upper"
    elif lower > 0 and upper > 0:
        group_type = "full"
    else:
        group_type = "upper"  # default for unrecognized calisthenics

    return {
        "type": group_type,
        "exercises": exercises,
        "cns_heavy": skill > 0,
        "sets": total_sets,
        "reps": total_reps,
    }


# ── Date helpers ───────────────────────────────────────────────────────────────

def _week_range(weeks_ago: int = 0) -> tuple[datetime, datetime]:
    """Return (start, end) for a given week relative to the most recent Monday (WIB).

    weeks_ago=0 → current week (this Monday → now)
    weeks_ago=1 → last full week (Mon–Sun)
    """
    now = datetime.now(WIB)
    this_monday = (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    week_start = this_monday - timedelta(weeks=weeks_ago)
    week_end = now if weeks_ago == 0 else week_start + timedelta(days=7)
    return week_start, week_end


def _filter_activities(
    activities: list[dict],
    start: datetime,
    end: datetime,
    sport_filter: Optional[set] = None,
) -> list[dict]:
    result = []
    for a in activities:
        dt = datetime.fromisoformat(a["start_date"].replace("Z", "+00:00")).astimezone(WIB)
        if start <= dt < end:
            if sport_filter is None or (a.get("sport_type") or a.get("type", "")) in sport_filter:
                result.append(a)
    return result


def _day_index(activity: dict) -> int:
    """Return weekday index in WIB (0=Mon, 6=Sun)."""
    return datetime.fromisoformat(
        activity["start_date"].replace("Z", "+00:00")
    ).astimezone(WIB).weekday()


# ── Data collection ────────────────────────────────────────────────────────────

async def fetch_gym_details(gym_activities: list[dict], strava: StravaClient) -> list[dict]:
    """Fetch full activity detail for each gym session to get description."""
    results = []
    for act in gym_activities:
        try:
            detail = strava.get_activity(act["id"])
            classified = classify_muscle_group(detail.get("description") or "")
            results.append({
                "activity": act,
                "detail": detail,
                "classified": classified,
                "day_index": _day_index(act),
            })
        except Exception as e:
            print(f"Failed to fetch gym detail {act.get('id')}: {e}")
    return results


async def enrich_runs(runs: list[dict]) -> list[dict]:
    """Enrich each run with weather, zones, decoupling, adjusted pace, time context."""
    max_hr = _read_max_hr_from_kb()

    async def _enrich_one(run: dict) -> dict:
        start_latlng = run.get("start_latlng") or []
        lat = start_latlng[0] if len(start_latlng) >= 2 else None
        lon = start_latlng[1] if len(start_latlng) >= 2 else None
        start_time = datetime.fromisoformat(run["start_date"].replace("Z", "+00:00"))

        weather = await get_weather(lat, lon, start_time) if lat else {}
        time_ctx = get_time_context(start_time, lat, lon) if lat else ""
        decoupling = compute_aerobic_decoupling(run)
        zones = compute_hr_zones(run, max_hr=max_hr)

        dist_m = run.get("distance", 0)
        moving = run.get("moving_time", 0)
        pace_sec = (moving / (dist_m / 1000)) if dist_m and moving else None
        adj_pace = (
            heat_adjusted_pace(pace_sec, weather["temp_c"], weather["humidity_pct"])
            if pace_sec and weather.get("temp_c") and weather.get("humidity_pct")
            else None
        )

        return {
            "activity": run,
            "weather": weather,
            "time_context": time_ctx,
            "decoupling": decoupling,
            "hr_zones": zones,
            "pace_sec": pace_sec,
            "adj_pace_sec": adj_pace,
            "day_index": _day_index(run),
        }

    return list(await asyncio.gather(*[_enrich_one(r) for r in runs]))


# ── Analysis ───────────────────────────────────────────────────────────────────

def aggregate_zones(enriched_runs: list[dict]) -> dict[str, int]:
    """Aggregate HR zone distribution across all runs."""
    zone_counts: dict[str, int] = {}
    for r in enriched_runs:
        for zone, pct in r["hr_zones"].items():
            zone_counts[zone] = zone_counts.get(zone, 0) + pct
    total = sum(zone_counts.values())
    if not total:
        return {}
    return {z: round(c / total * 100) for z, c in zone_counts.items() if c > 0}


def analyze_weather_performance(enriched_runs: list[dict]) -> Optional[str]:
    """Find correlation between temp and HR/pace across all runs."""
    data = [
        (r["weather"]["temp_c"], r["activity"].get("average_heartrate"), r["pace_sec"])
        for r in enriched_runs
        if r["weather"].get("temp_c") and r["activity"].get("average_heartrate") and r["pace_sec"]
    ]
    if len(data) < 2:
        return None

    temps = [d[0] for d in data]
    min_i = temps.index(min(temps))
    max_i = temps.index(max(temps))

    if temps[min_i] == temps[max_i]:
        return None

    hr_diff = round(data[max_i][1] - data[min_i][1])
    pace_diff = round(data[max_i][2] - data[min_i][2])
    temp_diff = round(temps[max_i] - temps[min_i], 1)

    return (
        f"Di {round(temps[min_i])}°C: HR {int(data[min_i][1])} bpm, {_fmt_pace(data[min_i][2])}/km — "
        f"di {round(temps[max_i])}°C: HR {int(data[max_i][1])} bpm, {_fmt_pace(data[max_i][2])}/km. "
        f"Tiap +{temp_diff}°C nambah ~{abs(hr_diff)} bpm dan ~{abs(pace_diff)}s pace."
    )


def analyze_time_of_day(enriched_runs: list[dict]) -> Optional[dict]:
    """Compare morning vs evening/afternoon run efficiency."""
    morning = [
        r for r in enriched_runs
        if any(w in r["time_context"] for w in ("morning", "sunrise"))
    ]
    evening = [
        r for r in enriched_runs
        if any(w in r["time_context"] for w in ("evening", "afternoon", "sunset"))
    ]

    def avg_stats(runs: list[dict]) -> tuple:
        hrs = [r["activity"]["average_heartrate"] for r in runs if r["activity"].get("average_heartrate")]
        paces = [r["pace_sec"] for r in runs if r["pace_sec"]]
        return (
            round(sum(hrs) / len(hrs)) if hrs else None,
            round(sum(paces) / len(paces)) if paces else None,
            len(runs),
        )

    if not morning and not evening:
        return None

    m_hr, m_pace, m_count = avg_stats(morning)
    e_hr, e_pace, e_count = avg_stats(evening)
    return {
        "morning": {"hr": m_hr, "pace": m_pace, "count": m_count},
        "evening": {"hr": e_hr, "pace": e_pace, "count": e_count},
    }


def analyze_gym_run_interaction(
    gym_sessions: list[dict], enriched_runs: list[dict]
) -> list[str]:
    """Cross-reference gym and run days, return insight strings."""
    flags: list[str] = []
    run_by_day = {r["day_index"]: r for r in enriched_runs}
    gym_by_day = {g["day_index"]: g for g in gym_sessions}

    for day_idx, gym in gym_by_day.items():
        classified = gym["classified"]
        day_name = DAYS_SHORT[day_idx]

        # Same-day gym + run
        if day_idx in run_by_day:
            hr = run_by_day[day_idx]["activity"].get("average_heartrate")
            flags.append(
                f"⚠️ {day_name}: gym + lari di hari yang sama"
                + (f" — HR run {int(hr)} bpm" if hr else "")
            )

        # Leg/full body before a run the next day
        if classified["type"] in ("lower", "full"):
            next_day = (day_idx + 1) % 7
            if next_day in run_by_day:
                next_name = DAYS_SHORT[next_day]
                flags.append(
                    f"⚠️ {day_name} leg/full-body → lari {next_name} keesokan harinya. "
                    "Perhatikan apakah HR lebih tinggi dari biasa."
                )

        # CNS-heavy skill work within 48h of a longer run
        if classified["cns_heavy"]:
            for offset in (1, 2):
                check_day = (day_idx + offset) % 7
                if check_day in run_by_day:
                    dist_km = run_by_day[check_day]["activity"].get("distance", 0) / 1000
                    if dist_km >= 5:
                        run_name = DAYS_SHORT[check_day]
                        flags.append(
                            f"⚠️ Skill session {day_name} → long run {run_name} dalam "
                            f"{offset * 24} jam. CNS belum tentu fully recovered."
                        )
                    break

    # Positive flag if all gym was upper / skill (no leg load)
    if gym_sessions and all(g["classified"]["type"] in ("upper", "skill") for g in gym_sessions):
        flags.append("✅ Semua gym sesi upper body / skill — zero leg load, aman buat volume lari.")

    return flags if flags else ["✅ Tidak ada konflik timing gym × lari minggu ini."]


def _aerobic_goal_progress(enriched_runs: list[dict]) -> int:
    """Estimate % progress toward 6:00/km @ HR≤140. Returns 0–100."""
    target_pace_sec = 360.0  # 6:00/km
    start_pace_sec = 450.0   # rough starting baseline (~7:30/km)

    relevant = [
        r for r in enriched_runs
        if r["pace_sec"] and r["activity"].get("average_heartrate")
        and abs(r["activity"]["average_heartrate"] - 140) <= 10
    ] or [r for r in enriched_runs if r["pace_sec"]]

    if not relevant:
        return 0

    best = min(r["adj_pace_sec"] or r["pace_sec"] for r in relevant)
    progress = (start_pace_sec - best) / (start_pace_sec - target_pace_sec) * 100
    return max(0, min(100, round(progress)))


# ── Schedule grid ──────────────────────────────────────────────────────────────

def _build_schedule_grid(gym_sessions: list[dict], enriched_runs: list[dict]) -> str:
    """Build 7-day schedule grid as a text table."""
    run_by_day: dict[int, str] = {}
    for r in enriched_runs:
        dist_km = r["activity"].get("distance", 0) / 1000
        run_by_day[r["day_index"]] = f"🏃{dist_km:.0f}k"

    gym_by_day: dict[int, str] = {}
    for g in gym_sessions:
        label = TYPE_LABEL.get(g["classified"]["type"], "??")
        gym_by_day[g["day_index"]] = f"💪{label}"

    col_w = 6
    header = "".join(d.ljust(col_w) for d in DAYS_SHORT)
    run_row = "".join(run_by_day.get(i, "—").ljust(col_w) for i in range(7))
    gym_row = "".join(gym_by_day.get(i, "—").ljust(col_w) for i in range(7))

    return f"```\n{header}\n{run_row}\n{gym_row}\n```"


# ── Formatting helpers ─────────────────────────────────────────────────────────

def _fmt_pace(sec_km: float) -> str:
    m, s = divmod(int(sec_km), 60)
    return f"{m}:{s:02d}"


def _trunc(text: str, limit: int = 1024) -> str:
    """Truncate to Discord embed field limit."""
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


# ── Claude insight ─────────────────────────────────────────────────────────────

def _build_prompt(
    *, total_km, prev_km, run_count, gym_count,
    zones_agg, avg_decoupling, prev_avg_decoupling,
    avg_adj_pace, prev_avg_adj_pace, avg_hr,
    weather_insight, tod_analysis, gym_flags,
    goal_progress, weight_kg, goals_content, kb_content,
) -> str:
    lines = ["## Weekly Training Data"]
    lines.append(f"- Total km: {total_km:.1f} (vs {prev_km:.1f} minggu lalu)")
    lines.append(f"- Run sessions: {run_count}, Gym sessions: {gym_count}")
    if avg_hr:
        lines.append(f"- Avg HR: {avg_hr} bpm")
    if zones_agg:
        lines.append(f"- Zone distribution: {', '.join(f'{z}: {p}%' for z, p in zones_agg.items())}")
    if avg_decoupling is not None:
        prev = f" (vs {prev_avg_decoupling}% minggu lalu)" if prev_avg_decoupling else ""
        lines.append(f"- Avg aerobic decoupling: {avg_decoupling}%{prev}")
    if avg_adj_pace:
        prev = f" (vs {_fmt_pace(prev_avg_adj_pace)}/km)" if prev_avg_adj_pace else ""
        lines.append(f"- Heat-adjusted pace avg: {_fmt_pace(avg_adj_pace)}/km{prev}")
    if weather_insight:
        lines.append(f"- Weather correlation: {weather_insight}")
    if tod_analysis:
        m, e = tod_analysis["morning"], tod_analysis["evening"]
        if m["hr"] and e["hr"]:
            lines.append(
                f"- Morning ({m['count']}x): HR {m['hr']} bpm, {_fmt_pace(m['pace'])}/km "
                f"vs Evening ({e['count']}x): HR {e['hr']} bpm, {_fmt_pace(e['pace'])}/km"
            )
    if gym_flags:
        lines.append(f"- Gym×Run flags: {'; '.join(gym_flags)}")
    if weight_kg:
        lines.append(f"- Berat badan hari ini: {weight_kg} kg")
    lines.append(f"- Aerobic goal progress: {goal_progress}%")

    data_block = "\n".join(lines)

    return (
        "Kamu adalah personal running coach. Berikan weekly analysis dalam Bahasa Indonesia. "
        "Tulis 3–4 kalimat insight yang spesifik dan actionable. "
        "Lalu berikan rekomendasi konkret untuk minggu depan: target km, komposisi easy vs quality, "
        "dan scheduling gym jika relevan. Reference angka aktual. Jangan generik.\n\n"
        f"## Athlete Profile\n{kb_content or '(no profile)'}\n\n"
        f"## Training Goals\n{goals_content or '(no goals)'}\n\n"
        f"{data_block}"
    )


def _generate_insight(prompt: str, claude_client: anthropic.Anthropic) -> str:
    try:
        response = claude_client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(b.text for b in response.content if b.type == "text").strip()
    except Exception as e:
        print(f"Weekly insight generation failed: {e}")
        return ""


# ── Discord embed builder ──────────────────────────────────────────────────────

def _build_embed(
    *, week_start, total_km, prev_km, km_change_pct, run_count,
    gym_count, gym_details, avg_hr, zones_agg, avg_decoupling,
    prev_avg_decoupling, avg_adj_pace, prev_avg_adj_pace,
    weather_insight, tod_analysis, gym_flags, schedule_grid,
    goal_progress, weight_kg, insight,
) -> discord.Embed:
    week_end = week_start + timedelta(days=6)
    now_wib = datetime.now(WIB)
    display_end = min(now_wib, week_end)
    title = f"Weekly Review — {week_start.strftime('%-d %b')} – {display_end.strftime('%-d %b')}"

    gym_types = [g["classified"]["type"] for g in gym_details]
    gym_subtitle = " · ".join(
        f"{gym_types.count(t)}× {t}"
        for t in ("upper", "lower", "full", "skill")
        if gym_types.count(t) > 0
    )

    desc = "Running + Gym · 7 hari terakhir"
    if weight_kg:
        desc += f" · ⚖️ {weight_kg} kg"
    embed = discord.Embed(title=title, description=desc, color=0xFC4C02)

    # Volume
    change_str = ""
    if km_change_pct is not None:
        sign = "+" if km_change_pct >= 0 else ""
        change_str = f" ({sign}{km_change_pct}% vs minggu lalu)"
    gym_line = f"{gym_count} gym ({gym_subtitle})" if gym_subtitle else f"{gym_count} gym"
    embed.add_field(
        name="Volume",
        value=_trunc(f"**{total_km:.1f} km**{change_str}\n{run_count} runs · {gym_line}"),
        inline=False,
    )

    # Zone distribution
    if zones_agg:
        merged: dict[str, int] = {}
        for z, p in zones_agg.items():
            key = "Zone 4+" if z in ("Zone 4", "Zone 5") else z
            merged[key] = merged.get(key, 0) + p
        zone_str = "  ·  ".join(
            f"**{z}** {p}%"
            for z in ("Zone 1", "Zone 2", "Zone 3", "Zone 4+")
            if (p := merged.get(z, 0)) > 0
        )
        z2 = zones_agg.get("Zone 2", 0)
        flag = "" if z2 >= 75 else f"\n⚠️ Zone 2 {z2}% — target 80%"
        embed.add_field(name="Zone distribution", value=_trunc(zone_str + flag), inline=False)

    # Aerobic efficiency
    eff_lines = []
    if avg_adj_pace:
        trend = ""
        if prev_avg_adj_pace:
            diff = prev_avg_adj_pace - avg_adj_pace
            trend = f" ({'↑' if diff > 0 else '↓'}{abs(diff):.0f}s vs minggu lalu)"
        eff_lines.append(f"Heat-adjusted pace: **{_fmt_pace(avg_adj_pace)}/km**{trend}")
    if avg_decoupling is not None:
        trend = ""
        if prev_avg_decoupling is not None:
            diff = avg_decoupling - prev_avg_decoupling
            trend = f" ({'↑' if diff > 0 else '↓'}{abs(diff):.1f}% vs minggu lalu)"
        eff_lines.append(f"Avg decoupling: **{avg_decoupling}%**{trend}")
    bar_filled = round(goal_progress / 10)
    bar = "█" * bar_filled + "░" * (10 - bar_filled)
    eff_lines.append(f"Goal (6:00/km @ HR≤140): `{bar}` {goal_progress}%")
    embed.add_field(name="Aerobic efficiency", value=_trunc("\n".join(eff_lines)), inline=False)

    # Gym × Run
    embed.add_field(
        name="Gym × Run",
        value=_trunc(schedule_grid + "\n" + "\n".join(gym_flags)),
        inline=False,
    )

    # Weather × Performance
    if weather_insight:
        embed.add_field(name="Cuaca × performa", value=_trunc(weather_insight), inline=False)

    # Time of day
    if tod_analysis:
        tod_lines = []
        m, e = tod_analysis["morning"], tod_analysis["evening"]
        if m["count"] and m["hr"] and m["pace"]:
            tod_lines.append(f"Pagi ({m['count']}x): HR **{m['hr']}** · **{_fmt_pace(m['pace'])}/km**")
        if e["count"] and e["hr"] and e["pace"]:
            tod_lines.append(f"Sore ({e['count']}x): HR **{e['hr']}** · **{_fmt_pace(e['pace'])}/km**")
        if tod_lines:
            embed.add_field(name="Pagi vs sore", value=_trunc("\n".join(tod_lines)), inline=False)

    # Claude insight + recommendation
    if insight:
        embed.add_field(name="Insight & rekomendasi", value=_trunc(insight), inline=False)

    embed.set_footer(
        text=f"{run_count} runs · {gym_count} gym · fitness-coach weekly digest · "
             f"{week_start.strftime('%-d %b')} – {week_end.strftime('%-d %b %Y')}"
    )
    return embed


# ── Main entry point ───────────────────────────────────────────────────────────

async def generate_weekly_analysis(
    activities: list[dict],
    strava: StravaClient,
    kb_content: str,
    goals_content: str,
    claude_client: anthropic.Anthropic,
    weight_kg: Optional[float] = None,
) -> discord.Embed:
    """Aggregate a full week of run + gym data and return a Discord embed."""
    week_start, week_end = _week_range(weeks_ago=0)
    prev_start, prev_end = _week_range(weeks_ago=1)

    this_runs = _filter_activities(activities, week_start, week_end, RUN_SPORTS)
    prev_runs = _filter_activities(activities, prev_start, prev_end, RUN_SPORTS)
    this_gym = _filter_activities(activities, week_start, week_end, GYM_SPORTS)

    enriched_this, enriched_prev, gym_details = await asyncio.gather(
        enrich_runs(this_runs),
        enrich_runs(prev_runs),
        fetch_gym_details(this_gym, strava),
    )

    # Aggregates
    zones_agg = aggregate_zones(enriched_this)
    weather_insight = analyze_weather_performance(enriched_this)
    tod_analysis = analyze_time_of_day(enriched_this)
    gym_flags = analyze_gym_run_interaction(gym_details, enriched_this)
    schedule_grid = _build_schedule_grid(gym_details, enriched_this)
    goal_progress = _aerobic_goal_progress(enriched_this)

    total_km = sum(r["activity"].get("distance", 0) for r in enriched_this) / 1000
    prev_km = sum(r["activity"].get("distance", 0) for r in enriched_prev) / 1000
    km_change_pct = round((total_km - prev_km) / prev_km * 100) if prev_km else None

    decouplings = [r["decoupling"] for r in enriched_this if r["decoupling"] is not None]
    prev_decouplings = [r["decoupling"] for r in enriched_prev if r["decoupling"] is not None]
    avg_decoupling = round(sum(decouplings) / len(decouplings), 1) if decouplings else None
    prev_avg_decoupling = round(sum(prev_decouplings) / len(prev_decouplings), 1) if prev_decouplings else None

    adj_paces = [r["adj_pace_sec"] for r in enriched_this if r["adj_pace_sec"]]
    prev_adj_paces = [r["adj_pace_sec"] for r in enriched_prev if r["adj_pace_sec"]]
    avg_adj_pace = round(sum(adj_paces) / len(adj_paces)) if adj_paces else None
    prev_avg_adj_pace = round(sum(prev_adj_paces) / len(prev_adj_paces)) if prev_adj_paces else None

    hr_vals = [r["activity"]["average_heartrate"] for r in enriched_this if r["activity"].get("average_heartrate")]
    avg_hr = round(sum(hr_vals) / len(hr_vals)) if hr_vals else None

    # Claude
    prompt = _build_prompt(
        total_km=total_km, prev_km=prev_km, run_count=len(this_runs),
        gym_count=len(this_gym), zones_agg=zones_agg,
        avg_decoupling=avg_decoupling, prev_avg_decoupling=prev_avg_decoupling,
        avg_adj_pace=avg_adj_pace, prev_avg_adj_pace=prev_avg_adj_pace,
        avg_hr=avg_hr, weather_insight=weather_insight, tod_analysis=tod_analysis,
        gym_flags=gym_flags, goal_progress=goal_progress,
        weight_kg=weight_kg, goals_content=goals_content, kb_content=kb_content,
    )
    insight = _generate_insight(prompt, claude_client)

    return _build_embed(
        week_start=week_start, total_km=total_km, prev_km=prev_km,
        km_change_pct=km_change_pct, run_count=len(this_runs),
        gym_count=len(this_gym), gym_details=gym_details, avg_hr=avg_hr,
        zones_agg=zones_agg, avg_decoupling=avg_decoupling,
        prev_avg_decoupling=prev_avg_decoupling, avg_adj_pace=avg_adj_pace,
        prev_avg_adj_pace=prev_avg_adj_pace, weather_insight=weather_insight,
        tod_analysis=tod_analysis, gym_flags=gym_flags, schedule_grid=schedule_grid,
        goal_progress=goal_progress, weight_kg=weight_kg, insight=insight,
    )
