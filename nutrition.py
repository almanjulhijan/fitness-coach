"""Nutrition tracking: food logging via Discord (photo/text), daily/weekly analysis."""

import base64
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import aiohttp
import anthropic
import discord

WIB = timezone(timedelta(hours=7))
LOGS_DIR = Path("nutrition_logs")
WEIGHT_FILE = Path("weight_log.json")
MODEL = "claude-sonnet-4-6"


# ── Weight tracking ──────────────────────────────────────────────────────────

def save_weight(kg: float, timestamp: datetime = None) -> dict:
    """Append a weight entry and return the saved entry."""
    ts = timestamp or datetime.now(WIB)
    entry = {"kg": round(kg, 1), "timestamp": ts.isoformat(), "date": ts.strftime("%Y-%m-%d")}

    entries = load_weight_history()
    entries.append(entry)
    WEIGHT_FILE.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
    return entry


def load_weight_history() -> list[dict]:
    """Load all weight entries from file."""
    if not WEIGHT_FILE.exists():
        return []
    try:
        return json.loads(WEIGHT_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, Exception):
        return []


def get_current_weight() -> Optional[float]:
    """Return the most recent weight in kg, or None."""
    entries = load_weight_history()
    if not entries:
        return None
    return entries[-1]["kg"]


def get_weight_trend(days: int = 30) -> list[dict]:
    """Return weight entries from the last N days, one per date (latest wins)."""
    cutoff = (datetime.now(WIB) - timedelta(days=days)).strftime("%Y-%m-%d")
    entries = load_weight_history()
    by_date = {}
    for e in entries:
        if e["date"] >= cutoff:
            by_date[e["date"]] = e
    return [by_date[d] for d in sorted(by_date.keys())]


def build_weight_embed(new_entry: dict) -> discord.Embed:
    """Build a Discord embed after logging a weight entry."""
    kg = new_entry["kg"]
    trend = get_weight_trend(days=30)

    embed = discord.Embed(
        title="⚖️ Berat dicatat: {} kg".format(kg),
        color=0xFF9800,
    )

    if len(trend) >= 2:
        first = trend[0]["kg"]
        delta = kg - first
        sign = "+" if delta >= 0 else ""
        period_days = (
            datetime.strptime(trend[-1]["date"], "%Y-%m-%d")
            - datetime.strptime(trend[0]["date"], "%Y-%m-%d")
        ).days
        embed.add_field(
            name="Trend ({} hari)".format(period_days),
            value="{}{:.1f} kg ({:.1f} → {:.1f})".format(sign, delta, first, kg),
            inline=True,
        )

        # Weekly average comparison
        week_entries = [e for e in trend if e["date"] >= (datetime.now(WIB) - timedelta(days=7)).strftime("%Y-%m-%d")]
        prev_entries = [e for e in trend if (datetime.now(WIB) - timedelta(days=14)).strftime("%Y-%m-%d") <= e["date"] < (datetime.now(WIB) - timedelta(days=7)).strftime("%Y-%m-%d")]
        if week_entries and prev_entries:
            avg_this = sum(e["kg"] for e in week_entries) / len(week_entries)
            avg_prev = sum(e["kg"] for e in prev_entries) / len(prev_entries)
            wk_delta = avg_this - avg_prev
            wk_sign = "+" if wk_delta >= 0 else ""
            embed.add_field(
                name="vs minggu lalu",
                value="{}{:.1f} kg (avg {:.1f} → {:.1f})".format(wk_sign, wk_delta, avg_prev, avg_this),
                inline=True,
            )

    # History lines (last 7 entries)
    if len(trend) > 1:
        history_lines = []
        for e in trend[-7:]:
            dt = datetime.strptime(e["date"], "%Y-%m-%d")
            history_lines.append("**{}** — {:.1f} kg".format(dt.strftime("%a %-d %b"), e["kg"]))
        embed.add_field(name="Riwayat", value="\n".join(history_lines), inline=False)

    embed.set_footer(text="Gunakan /weight <kg> untuk update")
    return embed


# ── Food analysis via Claude ──────────────────────────────────────────────────

def _build_food_prompt(text_description: str = "", has_image: bool = False) -> str:
    return (
        "Kamu adalah ahli nutrisi. Estimasi kandungan gizi dari makanan ini.\n\n"
        "Rules:\n"
        "- Estimasi kalori, protein, karbohidrat, lemak dalam angka\n"
        "- Kalau ada foto, analisa porsi dari visual\n"
        "- Kalau hanya text, estimasi berdasarkan porsi standar Indonesia\n"
        "- Berikan nama/deskripsi singkat makanan\n"
        "- RESPOND ONLY dengan valid JSON, tidak ada text lain\n\n"
        "Format JSON:\n"
        '{"food": "nama makanan", "calories": 450, "protein_g": 25, '
        '"carbs_g": 50, "fat_g": 15, "notes": "catatan singkat opsional"}\n\n'
        + (f"Deskripsi: {text_description}" if text_description else "Analisa dari foto.")
    )


async def analyze_food(
    claude_client: anthropic.Anthropic,
    text: str = "",
    image_data: bytes = None,
    image_media_type: str = "image/jpeg",
) -> dict:
    """Analyze food from text and/or image. Returns nutrition dict."""
    content = []

    if image_data:
        b64 = base64.standard_b64encode(image_data).decode("utf-8")
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": image_media_type, "data": b64},
        })

    content.append({"type": "text", "text": _build_food_prompt(text, bool(image_data))})

    try:
        response = claude_client.messages.create(
            model=MODEL,
            max_tokens=300,
            messages=[{"role": "user", "content": content}],
        )
        raw = "".join(b.text for b in response.content if b.type == "text").strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return json.loads(raw)
    except Exception as e:
        print("Food analysis failed: {}".format(e))
        return {}


# ── Storage ───────────────────────────────────────────────────────────────────

def _log_path(date: datetime) -> Path:
    return LOGS_DIR / date.strftime("%Y-%m-%d.json")


def save_food_entry(entry: dict, timestamp: datetime = None) -> None:
    """Append a food entry to today's log file."""
    LOGS_DIR.mkdir(exist_ok=True)
    ts = timestamp or datetime.now(WIB)
    entry["timestamp"] = ts.isoformat()
    entry["time_label"] = ts.strftime("%H:%M")

    path = _log_path(ts)
    entries = []
    if path.exists():
        try:
            entries = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, Exception):
            entries = []
    entries.append(entry)
    path.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")


def load_daily_entries(date: datetime = None) -> list[dict]:
    """Load all food entries for a given date."""
    dt = date or datetime.now(WIB)
    path = _log_path(dt)
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, Exception):
        return []


def load_entries_range(start: datetime, end: datetime) -> dict[str, list[dict]]:
    """Load food entries for a date range. Returns {date_str: [entries]}."""
    result = {}
    current = start
    while current <= end:
        entries = load_daily_entries(current)
        if entries:
            result[current.strftime("%Y-%m-%d")] = entries
        current += timedelta(days=1)
    return result


# ── Daily summary ─────────────────────────────────────────────────────────────

def compute_daily_totals(entries: list[dict]) -> dict:
    """Sum up calories and macros for a list of food entries."""
    totals = {"calories": 0, "protein_g": 0, "carbs_g": 0, "fat_g": 0, "count": len(entries)}
    for e in entries:
        totals["calories"] += e.get("calories", 0)
        totals["protein_g"] += e.get("protein_g", 0)
        totals["carbs_g"] += e.get("carbs_g", 0)
        totals["fat_g"] += e.get("fat_g", 0)
    return totals


def build_daily_embed(entries: list[dict], date: datetime = None) -> discord.Embed:
    """Build Discord embed for daily food summary."""
    dt = date or datetime.now(WIB)
    totals = compute_daily_totals(entries)

    embed = discord.Embed(
        title="🍽️ Food Log — {}".format(dt.strftime("%-d %b %Y")),
        description="{} entries hari ini".format(totals["count"]),
        color=0x4CAF50,
    )

    # Individual entries
    food_lines = []
    for e in entries:
        food_lines.append("**{}** {} — {} kcal (P{}g C{}g F{}g)".format(
            e.get("time_label", "??:??"),
            e.get("food", "unknown"),
            e.get("calories", 0),
            e.get("protein_g", 0),
            e.get("carbs_g", 0),
            e.get("fat_g", 0),
        ))
    if food_lines:
        embed.add_field(
            name="Entries",
            value="\n".join(food_lines[:10]),
            inline=False,
        )

    # Totals
    embed.add_field(
        name="Total",
        value="**{} kcal** · P {}g · C {}g · F {}g".format(
            totals["calories"], totals["protein_g"], totals["carbs_g"], totals["fat_g"]
        ),
        inline=False,
    )

    return embed


def build_food_reply_embed(entry: dict) -> discord.Embed:
    """Build a short reply embed after logging a food entry."""
    cal = entry.get("calories", 0)
    embed = discord.Embed(
        title="✅ {}".format(entry.get("food", "Food logged")),
        description="**{} kcal** · P {}g · C {}g · F {}g".format(
            cal, entry.get("protein_g", 0), entry.get("carbs_g", 0), entry.get("fat_g", 0),
        ),
        color=0x4CAF50,
    )
    if entry.get("notes"):
        embed.set_footer(text=entry["notes"])

    # Running total for today
    today_entries = load_daily_entries()
    today_totals = compute_daily_totals(today_entries)
    embed.add_field(
        name="Hari ini",
        value="**{} kcal** total dari {} entries".format(
            today_totals["calories"], today_totals["count"]
        ),
        inline=False,
    )
    return embed


# ── Holistic weight review ────────────────────────────────────────────────────

def _fmt_pace(sec_km: float) -> str:
    m, s = divmod(int(sec_km), 60)
    return "{}:{:02d}".format(m, s)


async def generate_weight_review(
    activities: list[dict],
    kb_content: str,
    goals_content: str,
    claude_client: anthropic.Anthropic,
) -> tuple[discord.Embed, str]:
    """Generate holistic weight loss review: nutrition + exercise + weight trend."""
    now = datetime.now(WIB)
    week_start = (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )

    # Nutrition data (this week)
    nutrition_data = load_entries_range(week_start, now)
    days_with_logs = len(nutrition_data)
    all_entries = [e for entries in nutrition_data.values() for e in entries]

    daily_cals = []
    daily_proteins = []
    for date_str, entries in sorted(nutrition_data.items()):
        totals = compute_daily_totals(entries)
        daily_cals.append(totals["calories"])
        daily_proteins.append(totals["protein_g"])

    avg_calories = round(sum(daily_cals) / len(daily_cals)) if daily_cals else 0
    avg_protein = round(sum(daily_proteins) / len(daily_proteins)) if daily_proteins else 0
    total_entries = len(all_entries)

    # Exercise data (this week)
    RUN_SPORTS = {"Run", "TrailRun", "VirtualRun"}
    week_runs = [
        a for a in activities
        if datetime.fromisoformat(a["start_date"].replace("Z", "+00:00")).astimezone(WIB) >= week_start
        and (a.get("sport_type") or a.get("type", "")) in RUN_SPORTS
    ]
    week_gym = [
        a for a in activities
        if datetime.fromisoformat(a["start_date"].replace("Z", "+00:00")).astimezone(WIB) >= week_start
        and (a.get("sport_type") or a.get("type", "")) in {"WeightTraining", "Workout"}
    ]

    run_km = sum(a.get("distance", 0) for a in week_runs) / 1000
    run_cals = sum(a.get("kilojoules", 0) or 0 for a in week_runs) / 4.184
    run_cals += sum(a.get("calories", 0) or 0 for a in week_runs if not a.get("kilojoules"))
    gym_sessions = len(week_gym)

    days_elapsed = (now - week_start).days + 1

    # Weight data
    current_weight = get_current_weight()
    weight_trend = get_weight_trend(days=30)

    # Build embed
    desc = "Evaluasi holistik: nutrisi + olahraga · {} hari".format(days_elapsed)
    if current_weight:
        desc += " · ⚖️ {:.1f} kg".format(current_weight)
    embed = discord.Embed(
        title="⚖️ Weight Review — minggu ini",
        description=desc,
        color=0xFF9800,
    )

    # Weight trend section
    if len(weight_trend) >= 2:
        first_w = weight_trend[0]
        last_w = weight_trend[-1]
        delta = last_w["kg"] - first_w["kg"]
        sign = "+" if delta >= 0 else ""
        weight_lines = [
            "**Sekarang:** {:.1f} kg".format(last_w["kg"]),
            "**30 hari:** {}{:.1f} kg ({:.1f} → {:.1f})".format(sign, delta, first_w["kg"], last_w["kg"]),
        ]
        embed.add_field(name="⚖️ Berat Badan", value="\n".join(weight_lines), inline=False)
    elif current_weight:
        embed.add_field(name="⚖️ Berat Badan", value="**{:.1f} kg** (belum cukup data trend)".format(current_weight), inline=False)

    # Nutrition section
    if daily_cals:
        nut_lines = []
        for date_str in sorted(nutrition_data.keys()):
            entries = nutrition_data[date_str]
            totals = compute_daily_totals(entries)
            dt = datetime.strptime(date_str, "%Y-%m-%d")
            nut_lines.append("**{}** — {} kcal · P {}g · {} entries".format(
                dt.strftime("%a %-d"), totals["calories"], totals["protein_g"], len(entries)
            ))
        embed.add_field(
            name="🍽️ Nutrisi",
            value="\n".join(nut_lines) + "\n**Rata-rata: {} kcal/hari · P {}g**".format(
                avg_calories, avg_protein
            ),
            inline=False,
        )
    else:
        embed.add_field(
            name="🍽️ Nutrisi",
            value="Belum ada food log minggu ini",
            inline=False,
        )

    # Exercise section
    exercise_lines = [
        "🏃 {} run · {:.1f} km".format(len(week_runs), run_km),
        "💪 {} gym session".format(gym_sessions),
    ]
    if run_cals > 0:
        exercise_lines.append("🔥 ~{:.0f} kcal burned (running)".format(run_cals))
    embed.add_field(name="🏋️ Olahraga", value="\n".join(exercise_lines), inline=False)

    # Build Claude prompt for holistic analysis
    nutrition_summary = ""
    if daily_cals:
        per_day_lines = []
        for date_str in sorted(nutrition_data.keys()):
            entries = nutrition_data[date_str]
            totals = compute_daily_totals(entries)
            foods = ", ".join(e.get("food", "?") for e in entries)
            per_day_lines.append("- {}: {} kcal, P {}g, C {}g, F {}g — {}".format(
                date_str, totals["calories"], totals["protein_g"],
                totals["carbs_g"], totals["fat_g"], foods
            ))
        nutrition_summary = "\n".join(per_day_lines)

    exercise_summary = (
        f"- Running: {len(week_runs)} runs, {run_km:.1f} km, ~{run_cals:.0f} kcal burned\n"
        f"- Gym: {gym_sessions} sessions"
    )

    weight_summary = ""
    if weight_trend:
        weight_lines = []
        for w in weight_trend:
            weight_lines.append("- {}: {:.1f} kg".format(w["date"], w["kg"]))
        weight_summary = "\n".join(weight_lines)
        if len(weight_trend) >= 2:
            delta = weight_trend[-1]["kg"] - weight_trend[0]["kg"]
            weight_summary += "\nTrend 30 hari: {}{:.1f} kg".format("+" if delta >= 0 else "", delta)
    elif current_weight:
        weight_summary = "Berat saat ini: {:.1f} kg (belum ada history)".format(current_weight)

    prompt = (
        "Kamu adalah personal coach untuk weight loss yang juga paham olahraga. "
        "Analisa data nutrisi + olahraga + berat badan minggu ini secara holistik. Bahasa Indonesia, lo/gue.\n\n"
        "## Athlete Profile\n{}\n\n"
        "## Goals\n{}\n\n"
        "## Berat Badan (30 hari terakhir)\n{}\n\n"
        "## Nutrisi minggu ini ({} hari dengan log)\n{}\n"
        "Rata-rata: {} kcal/hari, protein {} g/hari\n\n"
        "## Olahraga minggu ini\n{}\n\n"
        "Berikan analisa dalam format:\n\n"
        "**Evaluasi:**\n"
        "Apakah intake kalori mendukung target weight loss? "
        "Apakah protein cukup? Apakah deficit terlalu besar atau kurang? "
        "Bagaimana nutrisi terhadap beban latihan? "
        "Bagaimana trend berat badan?\n\n"
        "**Terus lakukan:**\n- (2-3 hal)\n\n"
        "**Stop/kurangi:**\n- (1-2 hal)\n\n"
        "**Mulai lakukan:**\n- (2-3 hal)\n\n"
        "Reference angka aktual. Jangan generik. Maksimal 350 kata.\n\n"
        "PENTING FORMAT:\n"
        "- Jangan tulis heading (# atau ##)\n"
        "- Jangan pakai tabel markdown (| kolom |)\n"
        "- Gunakan **bold** untuk label, bullet list untuk daftar"
    ).format(
        kb_content or "(no profile)",
        goals_content or "(no goals)",
        weight_summary or "(belum ada data berat)",
        days_with_logs,
        nutrition_summary or "(tidak ada data)",
        avg_calories, avg_protein,
        exercise_summary,
    )

    try:
        response = claude_client.messages.create(
            model=MODEL,
            max_tokens=800,
            messages=[{"role": "user", "content": prompt}],
        )
        insight = "".join(b.text for b in response.content if b.type == "text").strip()
    except Exception as e:
        print("Weight review insight failed: {}".format(e))
        insight = ""

    if insight:
        if len(insight) <= 1024:
            embed.add_field(name="Analisa & Rekomendasi", value=insight, inline=False)
        else:
            embed.add_field(
                name="Analisa & Rekomendasi",
                value="*Detail lengkap di thread bawah* ↓",
                inline=False,
            )

    embed.set_footer(text="Data minggu ini · nutrisi + Strava")

    return embed, insight
