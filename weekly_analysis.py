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
import supabase_client as supa

WIB = timezone(timedelta(hours=7))
RUN_SPORTS = {"Run", "TrailRun", "VirtualRun"}
GYM_SPORTS = {"WeightTraining", "Workout", "Crossfit"}

MODEL = "claude-sonnet-4-6"
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

        # Leg/full body before a run the next day — compare HR
        if classified["type"] in ("lower", "full"):
            next_day = (day_idx + 1) % 7
            if next_day in run_by_day:
                next_run = run_by_day[next_day]
                next_name = DAYS_SHORT[next_day]
                post_gym_hr = next_run["activity"].get("average_heartrate")

                other_hrs = [
                    r["activity"]["average_heartrate"]
                    for d, r in run_by_day.items()
                    if d != next_day and r["activity"].get("average_heartrate")
                ]
                avg_other_hr = round(sum(other_hrs) / len(other_hrs)) if other_hrs else None

                if post_gym_hr and avg_other_hr:
                    delta = int(post_gym_hr) - avg_other_hr
                    if delta > 5:
                        flags.append(
                            f"⚠️ {day_name} leg/full-body → lari {next_name}: "
                            f"HR **{int(post_gym_hr)}** bpm, +{delta} bpm di atas rata-rata ({avg_other_hr}). "
                            f"Fatigue dari gym kemungkinan carry over."
                        )
                    else:
                        flags.append(
                            f"✅ {day_name} leg/full-body → lari {next_name}: "
                            f"HR **{int(post_gym_hr)}** bpm, normal (rata-rata {avg_other_hr}). "
                            f"Recovery cukup."
                        )
                elif post_gym_hr:
                    flags.append(
                        f"⚠️ {day_name} leg/full-body → lari {next_name}: "
                        f"HR {int(post_gym_hr)} bpm. Tidak cukup data lari lain minggu ini untuk bandingkan."
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


def _aerobic_goal_progress(enriched_runs: list[dict]) -> dict:
    """Evaluate training alignment toward 6:00/km @ HR≤140.

    Returns dict with best_pace_sec, best_hr, avg_decoupling for runs near target HR.
    """
    near_target = [
        r for r in enriched_runs
        if r["pace_sec"] and r["activity"].get("average_heartrate")
        and abs(r["activity"]["average_heartrate"] - 140) <= 15
    ]

    decouplings = [r["decoupling"] for r in enriched_runs if r["decoupling"] is not None]
    avg_dc = round(sum(decouplings) / len(decouplings), 1) if decouplings else None

    if not near_target:
        return {"best_pace_sec": None, "best_hr": None, "avg_decoupling": avg_dc}

    best_run = min(near_target, key=lambda r: r["adj_pace_sec"] or r["pace_sec"])
    return {
        "best_pace_sec": best_run["adj_pace_sec"] or best_run["pace_sec"],
        "best_hr": int(best_run["activity"]["average_heartrate"]),
        "avg_decoupling": avg_dc,
    }


# ── Schedule grid ──────────────────────────────────────────────────────────────

def _build_schedule_grid(gym_sessions: list[dict], enriched_runs: list[dict]) -> str:
    """Build 7-day schedule as a mobile-friendly bullet list."""
    run_by_day: dict[int, str] = {}
    for r in enriched_runs:
        dist_km = r["activity"].get("distance", 0) / 1000
        run_by_day[r["day_index"]] = f"🏃 {dist_km:.0f}km"

    gym_by_day: dict[int, str] = {}
    for g in gym_sessions:
        label = TYPE_LABEL.get(g["classified"]["type"], "??")
        gym_by_day[g["day_index"]] = f"💪 {label}"

    lines = []
    for i, day in enumerate(DAYS_SHORT):
        parts = []
        if i in run_by_day:
            parts.append(run_by_day[i])
        if i in gym_by_day:
            parts.append(gym_by_day[i])
        if parts:
            lines.append(f"**{day}** — {' · '.join(parts)}")

    return "\n".join(lines) if lines else "—"


# ── Formatting helpers ─────────────────────────────────────────────────────────

def _fmt_pace(sec_km: float) -> str:
    m, s = divmod(int(sec_km), 60)
    return f"{m}:{s:02d}"


def _weight_context_str(current: float | None, historical: float | None) -> str:
    if not current:
        return ""
    line = f"- Berat badan terkini: {current:.1f} kg"
    if historical and abs(current - historical) >= 0.3:
        sign = "+" if current > historical else ""
        line += f" ({sign}{current - historical:.1f} kg vs 30 hari lalu)"
    return line


def _trunc(text: str, limit: int = 1024) -> str:
    """Truncate to Discord embed field limit."""
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


# ── Goal reflection ───────────────────────────────────────────────────────────

def compute_goal_reflection(
    enriched_runs: list[dict],
    run_count: int,
    zones_agg: dict,
    km_change_pct: Optional[int],
    goal_progress: dict,
    is_current_week: bool = False,
) -> str:
    """Return a formatted goal reflection string for the Discord embed field."""
    checks: list[str] = []
    icon = {"ok": "✅", "warn": "⚠️", "flag": "❌"}

    # 1. Consistency: 3–5 sessions per week
    if run_count >= 3 and run_count <= 5:
        checks.append(f"{icon['ok']} **Konsistensi:** {run_count} sesi — target 3–5x terpenuhi.")
    elif run_count < 3:
        if is_current_week:
            checks.append(f"{icon['warn']} **Konsistensi:** {run_count} sesi sejauh ini — target 3–5x, minggu masih berjalan.")
        else:
            checks.append(f"{icon['flag']} **Konsistensi:** {run_count} sesi — di bawah minimum 3x/minggu.")
    else:
        checks.append(f"{icon['warn']} **Konsistensi:** {run_count} sesi — di atas 5x, pastikan recovery cukup.")

    # 2. Intensity: Zone 2 target ≥80%
    z2 = zones_agg.get("Zone 2", 0)
    if z2 >= 80:
        checks.append(f"{icon['ok']} **Intensitas:** {z2}% di Zone 2 — sesuai target 80% easy.")
    elif z2 >= 65:
        checks.append(f"{icon['warn']} **Intensitas:** {z2}% di Zone 2, target 80%. Terlalu sering push ke Zone 3+.")
    elif z2 > 0:
        checks.append(f"{icon['flag']} **Intensitas:** hanya {z2}% di Zone 2 — mayoritas lari terlalu keras.")

    # 3. Load spike: max 10% increase recommended
    if km_change_pct is None:
        pass
    elif km_change_pct <= 10:
        checks.append(f"{icon['ok']} **Load:** volume naik {km_change_pct}% — dalam batas aman ≤10%.")
    elif km_change_pct <= 20:
        checks.append(f"{icon['warn']} **Load spike:** +{km_change_pct}% dari minggu lalu — sedikit di atas batas aman.")
    elif km_change_pct > 20:
        checks.append(f"{icon['flag']} **Load spike:** +{km_change_pct}% dari minggu lalu — risiko overtraining, turunkan minggu depan.")
    elif km_change_pct < 0:
        checks.append(f"{icon['warn']} **Load:** volume turun {abs(km_change_pct)}% — ok kalau recovery week, monitor konsistensi.")

    # 4. Quality session: check if any run had Zone 3+ effort > 20%
    quality_runs = [
        r for r in enriched_runs
        if (r["hr_zones"].get("Zone 3", 0) + r["hr_zones"].get("Zone 4", 0) + r["hr_zones"].get("Zone 5", 0)) > 20
    ]
    if quality_runs:
        checks.append(f"{icon['ok']} **Quality session:** ada {len(quality_runs)} sesi dengan effort Zone 3+ — target minimum 1x/minggu terpenuhi.")
    else:
        if is_current_week:
            checks.append(f"{icon['warn']} **Quality session:** belum ada sesi quality minggu ini — pertimbangkan 1 tempo atau interval.")
        else:
            checks.append(f"{icon['warn']} **Quality session:** tidak ada sesi dengan effort tinggi — pertimbangkan 1 tempo atau interval per minggu.")

    # 5. Aerobic goal: training alignment toward 6:00/km @ HR≤140
    gp = goal_progress
    best_pace = gp.get("best_pace_sec")
    best_hr = gp.get("best_hr")
    avg_decoupling = gp.get("avg_decoupling")
    alignment_issues = []

    if z2 < 80:
        alignment_issues.append("Zone 2 kurang ({}%, target ≥80%)".format(z2))
    if avg_decoupling is not None and avg_decoupling > 7:
        alignment_issues.append("decoupling tinggi ({:.1f}%, target <5%)".format(avg_decoupling))

    if best_pace and best_hr:
        pace_str = _fmt_pace(best_pace)
        if best_pace <= 360:
            checks.append(f"{icon['ok']} **Aerobic goal:** best pace {pace_str}/km @ HR {best_hr} — target 6:00/km @ HR≤140 sudah tercapai!")
        elif best_pace <= 420:
            checks.append(f"{icon['ok']} **Aerobic goal:** best easy pace {pace_str}/km @ HR {best_hr} — mendekati target 6:00/km, on track.")
        else:
            checks.append(f"{icon['warn']} **Aerobic goal:** best easy pace {pace_str}/km @ HR {best_hr} — masih jauh dari 6:00/km, konsisten di Zone 2.")
    else:
        checks.append(f"{icon['warn']} **Aerobic goal:** belum ada run di dekat HR 140 minggu ini untuk track progress.")

    if alignment_issues:
        checks.append(f"{icon['warn']} **Training alignment:** {'; '.join(alignment_issues)}")

    return "\n".join(checks)


# ── Claude insight ─────────────────────────────────────────────────────────────

def _build_prompt(
    *, total_km, prev_km, run_count, gym_count,
    zones_agg, avg_decoupling, prev_avg_decoupling,
    avg_adj_pace, prev_avg_adj_pace, avg_hr,
    weather_insight, tod_analysis, gym_flags,
    goal_progress, weight_kg, goals_content, kb_content,
    week_start, week_end, is_current_week=False,
) -> str:
    display_end = min(datetime.now(WIB), week_end)
    week_label = f"{week_start.strftime('%-d %b')} – {display_end.strftime('%-d %b %Y')}"
    if is_current_week:
        week_label += " (minggu berjalan, belum selesai)"
    lines = [f"## Weekly Training Data ({week_label})"]
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
        lines.append(f"- Berat badan terkini: {weight_kg} kg")
    gp_pace = goal_progress.get("best_pace_sec")
    gp_hr = goal_progress.get("best_hr")
    if gp_pace and gp_hr:
        lines.append(f"- Aerobic goal: best easy pace {_fmt_pace(gp_pace)}/km @ HR {gp_hr} (target 6:00/km @ HR≤140)")
    else:
        lines.append("- Aerobic goal: belum ada run di dekat HR target minggu ini")

    data_block = "\n".join(lines)

    if is_current_week:
        now_wib = datetime.now(WIB)
        days_left = 6 - now_wib.weekday()
        reco_instruction = (
            "Data ini adalah minggu yang SEDANG BERJALAN (belum selesai). "
            f"Masih ada {days_left} hari tersisa di minggu ini. "
            "Berikan rekomendasi konkret untuk SISA MINGGU INI: "
            "berapa km lagi yang perlu ditambah, sesi apa yang perlu dilakukan di hari-hari sisa, "
            "komposisi easy vs quality, dan scheduling gym jika relevan. "
            "JANGAN rekomendasikan untuk 'minggu depan' — fokus pada sisa minggu ini."
        )
    else:
        reco_instruction = (
            "Berikan rekomendasi konkret untuk minggu depan: target km, komposisi easy vs quality, "
            "dan scheduling gym jika relevan."
        )

    return (
        "Kamu adalah personal running coach. Berikan weekly analysis dalam Bahasa Indonesia. "
        "Tulis 3–4 kalimat insight yang spesifik dan actionable. "
        f"{reco_instruction} Reference angka aktual. Jangan generik.\n\n"
        "PENTING FORMAT:\n"
        "- Jangan tulis heading (# atau ##) — judul sudah diset otomatis\n"
        "- Jangan pakai tabel markdown (| kolom |)\n"
        "- Gunakan **bold** untuk label, bullet list untuk daftar\n"
        "- Maksimal 400 kata\n\n"
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
    goal_progress, weight_kg, insight, goal_reflection,
    weight_trend=None, is_current_week=False,
) -> discord.Embed:
    week_end = week_start + timedelta(days=6)
    now_wib = datetime.now(WIB)
    display_end = min(now_wib, week_end)
    title = f"Weekly Review — {week_start.strftime('%-d %b')} – {display_end.strftime('%-d %b')}"
    if is_current_week:
        title += " (in progress)"

    gym_types = [g["classified"]["type"] for g in gym_details]
    gym_subtitle = " · ".join(
        f"{gym_types.count(t)}× {t}"
        for t in ("upper", "lower", "full", "skill")
        if gym_types.count(t) > 0
    )

    days_elapsed = (now_wib - week_start).days + 1 if is_current_week else 7
    desc = f"Running + Gym · {days_elapsed} hari"
    if is_current_week:
        desc += " (minggu berjalan)"
    if weight_kg:
        weight_str = f"⚖️ {weight_kg:.1f} kg"
        if weight_trend and weight_trend.get("change_kg") is not None:
            sign = "+" if weight_trend["change_kg"] >= 0 else ""
            weight_str += f" ({sign}{weight_trend['change_kg']:.1f})"
        desc += f" · {weight_str}"
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
    gp_pace = goal_progress.get("best_pace_sec")
    gp_hr = goal_progress.get("best_hr")
    if gp_pace and gp_hr:
        eff_lines.append(f"Goal (6:00/km @ HR≤140): best **{_fmt_pace(gp_pace)}/km** @ HR {gp_hr}")
    else:
        eff_lines.append("Goal (6:00/km @ HR≤140): belum ada data di dekat HR target")
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

    # Goal reflection
    if goal_reflection:
        embed.add_field(name="Goal reflection", value=_trunc(goal_reflection), inline=False)

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
    weeks_ago: int = 1,
) -> tuple[discord.Embed, str, str]:
    """Aggregate a full week of run + gym data and return a Discord embed.

    weeks_ago=0 → current (in-progress) week, weeks_ago=1 → last completed week.
    """
    week_start, week_end = _week_range(weeks_ago=weeks_ago)
    prev_start, prev_end = _week_range(weeks_ago=weeks_ago + 1)

    if weight_kg is None:
        if weeks_ago == 0:
            weight_kg = supa.get_latest_weight()
        else:
            weight_kg = supa.get_weight_at(week_end)
    weight_trend = supa.get_weight_trend(weeks=4)

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

    # Goal reflection
    goal_reflection = compute_goal_reflection(
        enriched_runs=enriched_this,
        run_count=len(this_runs),
        zones_agg=zones_agg,
        km_change_pct=km_change_pct,
        goal_progress=goal_progress,
        is_current_week=(weeks_ago == 0),
    )

    # Claude
    prompt = _build_prompt(
        total_km=total_km, prev_km=prev_km, run_count=len(this_runs),
        gym_count=len(this_gym), zones_agg=zones_agg,
        avg_decoupling=avg_decoupling, prev_avg_decoupling=prev_avg_decoupling,
        avg_adj_pace=avg_adj_pace, prev_avg_adj_pace=prev_avg_adj_pace,
        avg_hr=avg_hr, weather_insight=weather_insight, tod_analysis=tod_analysis,
        gym_flags=gym_flags, goal_progress=goal_progress,
        weight_kg=weight_kg, goals_content=goals_content, kb_content=kb_content,
        week_start=week_start, week_end=week_end,
        is_current_week=(weeks_ago == 0),
    )
    insight = _generate_insight(prompt, claude_client)

    embed = _build_embed(
        week_start=week_start, total_km=total_km, prev_km=prev_km,
        km_change_pct=km_change_pct, run_count=len(this_runs),
        gym_count=len(this_gym), gym_details=gym_details, avg_hr=avg_hr,
        zones_agg=zones_agg, avg_decoupling=avg_decoupling,
        prev_avg_decoupling=prev_avg_decoupling, avg_adj_pace=avg_adj_pace,
        prev_avg_adj_pace=prev_avg_adj_pace, weather_insight=weather_insight,
        tod_analysis=tod_analysis, gym_flags=gym_flags, schedule_grid=schedule_grid,
        goal_progress=goal_progress, weight_kg=weight_kg, insight=insight,
        goal_reflection=goal_reflection, weight_trend=weight_trend,
        is_current_week=(weeks_ago == 0),
    )

    supa.save_weekly_snapshot({
        "week_start": week_start.strftime("%Y-%m-%d"),
        "week_end": (week_start + timedelta(days=6)).strftime("%Y-%m-%d"),
        "total_km": round(total_km, 2),
        "run_count": len(this_runs),
        "gym_count": len(this_gym),
        "avg_hr": avg_hr,
        "zones_agg": zones_agg or None,
        "avg_decoupling": avg_decoupling,
        "avg_adj_pace_sec": avg_adj_pace,
        "weight_kg": weight_kg,
        "insight": insight or None,
        "goal_reflection": goal_reflection or None,
    })

    # Build plain-text summary for conversation history injection
    zone_str = ", ".join(f"{z}: {p}%" for z, p in zones_agg.items()) if zones_agg else "—"
    summary_lines = [
        f"[Weekly analysis posted — {week_start.strftime('%-d %b')} s/d {datetime.now(WIB).strftime('%-d %b')}]",
        f"- Volume: {total_km:.1f} km ({len(this_runs)} runs, {len(this_gym)} gym)",
        f"- Avg HR: {avg_hr} bpm" if avg_hr else "",
        f"- Zone distribution: {zone_str}",
        f"- Heat-adjusted pace: {_fmt_pace(avg_adj_pace)}/km" if avg_adj_pace else "",
        f"- Aerobic decoupling: {avg_decoupling}%" if avg_decoupling is not None else "",
        f"- Aerobic goal: best {_fmt_pace(goal_progress['best_pace_sec'])}/km @ HR {goal_progress['best_hr']}" if goal_progress.get("best_pace_sec") else "- Aerobic goal: belum ada data",
        f"- Gym×Run flags: {'; '.join(gym_flags)}" if gym_flags else "",
        f"- Insight: {insight}" if insight else "",
    ]
    summary = "\n".join(l for l in summary_lines if l)

    return embed, insight, summary


# ── Zone 2 progress review ────────────────────────────────────────────────────

async def generate_zone2_review(
    activities: list[dict],
    kb_content: str,
    goals_content: str,
    claude_client: anthropic.Anthropic,
) -> tuple[discord.Embed, str]:
    """Analyze Zone 2 running trend over last 30 days, return embed + insight."""
    runs = [
        a for a in activities
        if (a.get("sport_type") or a.get("type", "")) in RUN_SPORTS
    ]
    if not runs:
        embed = discord.Embed(
            title="Zone 2 Progress Review",
            description="Tidak ada data lari dalam 30 hari terakhir.",
            color=0x949ba4,
        )
        return embed, ""

    enriched = await enrich_runs(runs)
    max_hr = _read_max_hr_from_kb()
    z2_upper = max_hr * 0.75

    # Group runs by week (Mon-Sun)
    now = datetime.now(WIB)
    weeks: dict[int, list[dict]] = {}
    for r in enriched:
        run_dt = datetime.fromisoformat(
            r["activity"]["start_date"].replace("Z", "+00:00")
        ).astimezone(WIB)
        weeks_ago = (now - run_dt).days // 7
        weeks.setdefault(weeks_ago, []).append(r)

    # Per-week stats
    week_stats = []
    for wk in sorted(weeks.keys()):
        wk_runs = weeks[wk]
        z2_runs = [r for r in wk_runs if r["hr_zones"].get("Zone 2", 0) >= 50]
        all_z2_pct = [r["hr_zones"].get("Zone 2", 0) for r in wk_runs]
        avg_z2_pct = round(sum(all_z2_pct) / len(all_z2_pct)) if all_z2_pct else 0

        paces = [r["adj_pace_sec"] or r["pace_sec"] for r in wk_runs if r["adj_pace_sec"] or r["pace_sec"]]
        avg_pace = round(sum(paces) / len(paces)) if paces else None

        z2_paces = [
            r["adj_pace_sec"] or r["pace_sec"]
            for r in z2_runs
            if r["adj_pace_sec"] or r["pace_sec"]
        ]
        avg_z2_pace = round(sum(z2_paces) / len(z2_paces)) if z2_paces else None

        hrs = [r["activity"]["average_heartrate"] for r in wk_runs if r["activity"].get("average_heartrate")]
        avg_hr = round(sum(hrs) / len(hrs)) if hrs else None

        dcs = [r["decoupling"] for r in wk_runs if r["decoupling"] is not None]
        avg_dc = round(sum(dcs) / len(dcs), 1) if dcs else None

        total_km = sum(r["activity"].get("distance", 0) for r in wk_runs) / 1000

        wk_start = now - timedelta(days=(wk + 1) * 7 - (now.weekday()))
        wk_label = wk_start.strftime("%-d %b")

        week_stats.append({
            "week_ago": wk,
            "label": wk_label,
            "total_runs": len(wk_runs),
            "z2_runs": len(z2_runs),
            "avg_z2_pct": avg_z2_pct,
            "avg_pace": avg_pace,
            "avg_z2_pace": avg_z2_pace,
            "avg_hr": avg_hr,
            "avg_decoupling": avg_dc,
            "total_km": round(total_km, 1),
        })

    # Overall Zone 2 stats
    all_z2_runs = [r for r in enriched if r["hr_zones"].get("Zone 2", 0) >= 50]
    total_z2 = len(all_z2_runs)
    total_runs = len(enriched)
    z2_ratio = round(total_z2 / total_runs * 100) if total_runs else 0

    best_z2_run = None
    if all_z2_runs:
        with_pace = [r for r in all_z2_runs if r["adj_pace_sec"] or r["pace_sec"]]
        if with_pace:
            best_z2_run = min(with_pace, key=lambda r: r["adj_pace_sec"] or r["pace_sec"])

    # Earliest vs latest Z2 comparison
    z2_with_pace = sorted(
        [r for r in all_z2_runs if r["adj_pace_sec"] or r["pace_sec"]],
        key=lambda r: r["activity"]["start_date"],
    )
    earliest_z2 = z2_with_pace[0] if z2_with_pace else None
    latest_z2 = z2_with_pace[-1] if len(z2_with_pace) > 1 else None

    # Weight context for earliest vs latest comparison
    weight_now = supa.get_latest_weight()
    weight_30d_ago = None
    if earliest_z2:
        earliest_dt = datetime.fromisoformat(
            earliest_z2["activity"]["start_date"].replace("Z", "+00:00")
        )
        weight_30d_ago = supa.get_weight_at(earliest_dt)

    # Build embed
    desc = f"Analisa {total_runs} run dalam 30 hari terakhir · {total_z2} run dominan Zone 2 ({z2_ratio}%)"
    if weight_now:
        desc += f" · ⚖️ {weight_now:.1f} kg"
        if weight_30d_ago and abs(weight_now - weight_30d_ago) >= 0.5:
            sign = "+" if weight_now > weight_30d_ago else ""
            desc += f" ({sign}{weight_now - weight_30d_ago:.1f} vs 30d lalu)"
    embed = discord.Embed(
        title="🫀 Zone 2 Progress Review",
        description=desc,
        color=0x5de08a,
    )

    # Weekly trend
    trend_lines = []
    for ws in reversed(week_stats):
        pace_str = _fmt_pace(ws["avg_z2_pace"]) + "/km" if ws["avg_z2_pace"] else "—"
        dc_str = f"{ws['avg_decoupling']}%" if ws["avg_decoupling"] is not None else "—"
        label = "minggu ini" if ws["week_ago"] == 0 else f"{ws['label']}"
        trend_lines.append(
            f"**{label}** — {ws['z2_runs']}/{ws['total_runs']} Z2 · "
            f"pace {pace_str} · HR {ws['avg_hr'] or '—'} · dc {dc_str} · {ws['total_km']} km"
        )
    embed.add_field(
        name="Trend per minggu",
        value=_trunc("\n".join(trend_lines)),
        inline=False,
    )

    # Best Z2 run
    if best_z2_run:
        bp = best_z2_run["adj_pace_sec"] or best_z2_run["pace_sec"]
        bhr = int(best_z2_run["activity"]["average_heartrate"])
        bdc = best_z2_run["decoupling"]
        bdist = best_z2_run["activity"].get("distance", 0) / 1000
        bdate = datetime.fromisoformat(
            best_z2_run["activity"]["start_date"].replace("Z", "+00:00")
        ).astimezone(WIB).strftime("%-d %b")
        dc_str = f"dc {bdc}%" if bdc is not None else ""
        embed.add_field(
            name="Best Zone 2 run",
            value=f"**{_fmt_pace(bp)}/km** (adjusted) @ HR {bhr} · {bdist:.1f} km · {dc_str} · {bdate}",
            inline=False,
        )

    # Progress: earliest vs latest (normalized)
    if earliest_z2 and latest_z2:
        def _z2_snapshot(r):
            adj = r["adj_pace_sec"]
            raw = r["pace_sec"]
            hr = int(r["activity"]["average_heartrate"])
            dc = r["decoupling"]
            dist = r["activity"].get("distance", 0) / 1000
            dt = datetime.fromisoformat(r["activity"]["start_date"].replace("Z", "+00:00")).astimezone(WIB)
            weather = r.get("weather") or {}
            temp = weather.get("temp_c")
            return adj, raw, hr, dc, dist, dt, temp

        e_adj, e_raw, ehr, edc, edist, edt, etemp = _z2_snapshot(earliest_z2)
        l_adj, l_raw, lhr, ldc, ldist, ldt, ltemp = _z2_snapshot(latest_z2)

        # Use adjusted pace for both if available, otherwise raw for both
        if e_adj and l_adj:
            ep, lp = e_adj, l_adj
            pace_label = "adjusted"
        else:
            ep, lp = e_raw, l_raw
            pace_label = "raw"

        edc_str = f"dc {edc}%" if edc is not None else "dc —"
        ldc_str = f"dc {ldc}%" if ldc is not None else "dc —"
        etemp_str = f" · {round(etemp)}°C" if etemp is not None else ""
        ltemp_str = f" · {round(ltemp)}°C" if ltemp is not None else ""

        pace_diff = ep - lp
        days_span = (ldt - edt).days

        progress_lines = [
            f"**{edt.strftime('%-d %b')}** (awal): {_fmt_pace(ep)}/km · HR {ehr} · {edc_str} · {edist:.1f} km{etemp_str}",
            f"**{ldt.strftime('%-d %b')}** (terkini): {_fmt_pace(lp)}/km · HR {lhr} · {ldc_str} · {ldist:.1f} km{ltemp_str}",
            f"*Pace: {pace_label} (heat-normalized)*" if pace_label == "adjusted" else "*Pace: raw (weather data tidak lengkap)*",
        ]

        # Distance-aware comparison
        dist_diff = abs(edist - ldist)
        if dist_diff > 2:
            progress_lines.append(f"*Catatan: jarak berbeda ({edist:.1f} vs {ldist:.1f} km) — pace jarak lebih jauh cenderung lebih lambat*")

        if pace_diff > 5:
            progress_lines.append(f"↑ Pace membaik **{int(pace_diff)}s/km** dalam {days_span} hari")
        elif pace_diff < -5:
            progress_lines.append(f"↓ Pace melambat **{int(abs(pace_diff))}s/km** dalam {days_span} hari")
        else:
            progress_lines.append(f"→ Pace stabil (±{int(abs(pace_diff))}s) dalam {days_span} hari")

        hr_diff = ehr - lhr
        if abs(hr_diff) > 2:
            hr_arrow = "↓" if hr_diff > 0 else "↑"
            progress_lines.append(f"{hr_arrow} HR {'turun' if hr_diff > 0 else 'naik'} **{abs(hr_diff)} bpm** — {'efisiensi membaik' if hr_diff > 0 else 'perlu perhatian'}")

        embed.add_field(
            name="Progres: awal → sekarang",
            value="\n".join(progress_lines),
            inline=False,
        )

    # Target comparison
    target_gap = ""
    if best_z2_run:
        bp = best_z2_run["adj_pace_sec"] or best_z2_run["pace_sec"]
        gap_sec = bp - 360
        if gap_sec > 0:
            target_gap = f"Gap ke target: **{_fmt_pace(gap_sec)}** lebih lambat dari 6:00/km"
        else:
            target_gap = "**Target 6:00/km @ HR≤140 sudah tercapai!**"
    embed.add_field(name="Goal: 6:00/km @ HR≤140", value=target_gap or "Belum ada data Z2", inline=False)

    # Build Claude prompt
    trend_data = "\n".join(
        f"- Minggu {ws['label']}: {ws['z2_runs']}/{ws['total_runs']} Z2 runs, "
        f"avg Z2 pace {_fmt_pace(ws['avg_z2_pace'])}/km, " if ws["avg_z2_pace"] else ""
        f"avg HR {ws['avg_hr']}, decoupling {ws['avg_decoupling']}%, {ws['total_km']} km"
        for ws in reversed(week_stats)
    )

    best_info = ""
    if best_z2_run:
        bp = best_z2_run["adj_pace_sec"] or best_z2_run["pace_sec"]
        best_info = f"Best Z2 run: {_fmt_pace(bp)}/km @ HR {int(best_z2_run['activity']['average_heartrate'])}, decoupling {best_z2_run['decoupling']}%"

    progress_info = ""
    if earliest_z2 and latest_z2:
        e_adj_p = earliest_z2["adj_pace_sec"]
        l_adj_p = latest_z2["adj_pace_sec"]
        if e_adj_p and l_adj_p:
            p_ep, p_lp = e_adj_p, l_adj_p
            p_method = "heat-adjusted"
        else:
            p_ep = earliest_z2["pace_sec"]
            p_lp = latest_z2["pace_sec"]
            p_method = "raw (cuaca tidak lengkap untuk semua run)"

        p_ehr = int(earliest_z2["activity"]["average_heartrate"])
        p_edc = earliest_z2["decoupling"]
        p_edist = earliest_z2["activity"].get("distance", 0) / 1000
        p_etemp = (earliest_z2.get("weather") or {}).get("temp_c")
        p_edt = datetime.fromisoformat(earliest_z2["activity"]["start_date"].replace("Z", "+00:00")).astimezone(WIB)

        p_lhr = int(latest_z2["activity"]["average_heartrate"])
        p_ldc = latest_z2["decoupling"]
        p_ldist = latest_z2["activity"].get("distance", 0) / 1000
        p_ltemp = (latest_z2.get("weather") or {}).get("temp_c")
        p_ldt = datetime.fromisoformat(latest_z2["activity"]["start_date"].replace("Z", "+00:00")).astimezone(WIB)

        pace_diff = p_ep - p_lp
        dc_e = f"{p_edc}%" if p_edc is not None else "N/A"
        dc_l = f"{p_ldc}%" if p_ldc is not None else "N/A"
        temp_e = f", {round(p_etemp)}°C" if p_etemp is not None else ""
        temp_l = f", {round(p_ltemp)}°C" if p_ltemp is not None else ""

        progress_info = (
            f"\n## Progres awal → sekarang (normalized: {p_method})\n"
            f"- Awal ({p_edt.strftime('%-d %b')}): {_fmt_pace(p_ep)}/km @ HR {p_ehr}, dc {dc_e}, {p_edist:.1f} km{temp_e}\n"
            f"- Terkini ({p_ldt.strftime('%-d %b')}): {_fmt_pace(p_lp)}/km @ HR {p_lhr}, dc {dc_l}, {p_ldist:.1f} km{temp_l}\n"
            f"- Perubahan pace: {'+' if pace_diff > 0 else ''}{int(pace_diff)}s/km dalam {(p_ldt - p_edt).days} hari\n"
            f"- Perubahan HR: {'+' if (p_lhr - p_ehr) > 0 else ''}{p_lhr - p_ehr} bpm\n"
            f"- PENTING: perbandingan ini sudah dinormalisasi. Pertimbangkan jarak ({p_edist:.1f} vs {p_ldist:.1f} km) "
            f"saat menginterpretasi — jarak lebih jauh cenderung lebih lambat."
        )

    prompt = (
        "Kamu adalah personal running coach. Analisa progres Zone 2 running atlet ini "
        "dalam 30 hari terakhir. Bahasa Indonesia.\n\n"
        "## Athlete Profile\n{}\n\n"
        "## Training Goals\n{}\n\n"
        "## Zone 2 Data (30 hari)\n"
        "- Total: {} run, {} dominan Zone 2 ({}%)\n"
        "- Max HR atlet: {} bpm, Zone 2 range: {}-{} bpm\n"
        "{}\n{}\n"
        "{}\n\n"
        "## Weekly trend\n{}\n"
        "{}\n\n"
        "Berikan analisa dalam format:\n\n"
        "**Progres Zone 2:**\n"
        "Jelaskan perjalanan progres Z2 atlet dari run paling awal sampai sekarang. "
        "Bandingkan pace, HR, dan decoupling awal vs terkini. "
        "Apakah membaik, stagnan, atau menurun? Sebutkan angka spesifik.\n\n"
        "**Terus lakukan:**\n- (2-3 hal spesifik yang sudah benar)\n\n"
        "**Stop/kurangi:**\n- (1-2 hal yang perlu dihentikan atau dikurangi)\n\n"
        "**Mulai lakukan:**\n- (2-3 hal baru yang perlu dilakukan untuk mencapai goal)\n\n"
        "Reference angka aktual dari data. Jangan generik. Maksimal 400 kata.\n\n"
        "PENTING FORMAT:\n"
        "- Jangan tulis heading (# atau ##)\n"
        "- Jangan pakai tabel markdown (| kolom |)\n"
        "- Gunakan **bold** untuk label, bullet list untuk daftar"
    ).format(
        kb_content or "(no profile)",
        goals_content or "(no goals)",
        total_runs, total_z2, z2_ratio,
        max_hr, int(max_hr * 0.60), int(z2_upper),
        best_info,
        f"- Z2 ratio target: ≥80%, current: {z2_ratio}%",
        _weight_context_str(weight_now, weight_30d_ago),
        trend_data,
        progress_info,
    )

    insight = _generate_insight(prompt, claude_client)

    if insight:
        if len(insight) <= 1024:
            embed.add_field(name="Analisa & Rekomendasi", value=insight, inline=False)
        else:
            embed.add_field(
                name="Analisa & Rekomendasi",
                value="*Detail lengkap di thread bawah* ↓",
                inline=False,
            )

    embed.set_footer(text="Data 30 hari terakhir dari Strava")

    return embed, insight
