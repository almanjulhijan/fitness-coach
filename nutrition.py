"""Nutrition tracking: food logging via Discord (photo/text), daily/weekly analysis."""

import base64
import json
from datetime import datetime, timedelta, timezone
from typing import Optional

import anthropic
import discord

import db

WIB = timezone(timedelta(hours=7))
MODEL = "claude-sonnet-4-6"


# ── Food analysis via Claude ──────────────────────────────────────────────────

def _build_food_prompt(text_description: str = "", has_image: bool = False) -> str:
    return (
        "Kamu adalah ahli nutrisi. Estimasi kandungan gizi dari makanan ini.\n\n"
        "Rules:\n"
        "- Estimasi kalori, protein, karbohidrat, lemak dalam angka\n"
        "- Kalau ada foto, ASUMSIKAN user makan SELURUH makanan yang terlihat di foto, bukan sebagian. "
        "Jangan pernah tulis 'sebagian dimakan' kecuali user secara eksplisit bilang begitu\n"
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

    today_entries = db.load_daily_entries()
    today_totals = compute_daily_totals(today_entries)
    embed.add_field(
        name="Hari ini",
        value="**{} kcal** total dari {} entries".format(
            today_totals["calories"], today_totals["count"]
        ),
        inline=False,
    )
    return embed


# ── Weight embed ─────────────────────────────────────────────────────────────

def build_weight_embed(new_entry: dict) -> discord.Embed:
    """Build a Discord embed after logging a weight entry."""
    kg = new_entry["kg"]
    trend = db.get_weight_trend(days=30)

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

    if len(trend) > 1:
        history_lines = []
        for e in trend[-7:]:
            dt = datetime.strptime(e["date"], "%Y-%m-%d")
            history_lines.append("**{}** — {:.1f} kg".format(dt.strftime("%a %-d %b"), e["kg"]))
        embed.add_field(name="Riwayat", value="\n".join(history_lines), inline=False)

    embed.set_footer(text="Gunakan /weight <kg> untuk update")
    return embed


# ── Holistic weight review ────────────────────────────────────────────────────

def _fmt_pace(sec_km: float) -> str:
    m, s = divmod(int(sec_km), 60)
    return "{}:{:02d}".format(m, s)


def _week_range(weeks_ago: int = 0) -> tuple[datetime, datetime]:
    now = datetime.now(WIB)
    this_monday = (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    week_start = this_monday - timedelta(weeks=weeks_ago)
    week_end = now if weeks_ago == 0 else week_start + timedelta(days=6, hours=23, minutes=59, seconds=59)
    return week_start, week_end


def _weight_staleness(weight_trend: list[dict]) -> tuple[str, int]:
    """Check how stale the latest weight entry is. Returns (status_text, days_old)."""
    if not weight_trend:
        return "⚠️ Belum pernah log berat badan. Gunakan `/weight <kg>`", -1
    last_date_str = weight_trend[-1]["date"]
    last_date = datetime.strptime(last_date_str, "%Y-%m-%d").replace(tzinfo=WIB)
    days_old = (datetime.now(WIB) - last_date).days
    if days_old <= 3:
        return "", days_old
    elif days_old <= 7:
        return "⚠️ Berat terakhir {} hari lalu — update yuk pakai `/weight`".format(days_old), days_old
    else:
        return "❌ Berat terakhir **{} hari lalu** — data sudah basi, update pakai `/weight`".format(days_old), days_old


async def generate_nutrition_weekly_review(
    activities: list[dict],
    kb_content: str,
    goals_content: str,
    claude_client: anthropic.Anthropic,
    weeks_ago: int = 0,
) -> tuple[discord.Embed, str]:
    """Generate weekly nutrition review with week context awareness."""
    now = datetime.now(WIB)
    week_start, week_end = _week_range(weeks_ago)
    is_current_week = (weeks_ago == 0)
    display_end = min(now, week_end)

    # Nutrition data
    nutrition_data = db.load_entries_range(week_start, display_end)
    days_with_logs = len(nutrition_data)

    daily_cals = []
    daily_proteins = []
    daily_carbs = []
    daily_fats = []
    for date_str, entries in sorted(nutrition_data.items()):
        totals = compute_daily_totals(entries)
        daily_cals.append(totals["calories"])
        daily_proteins.append(totals["protein_g"])
        daily_carbs.append(totals["carbs_g"])
        daily_fats.append(totals["fat_g"])

    avg_calories = round(sum(daily_cals) / len(daily_cals)) if daily_cals else 0
    avg_protein = round(sum(daily_proteins) / len(daily_proteins)) if daily_proteins else 0

    # Exercise data
    RUN_SPORTS = {"Run", "TrailRun", "VirtualRun"}
    week_runs = [
        a for a in activities
        if datetime.fromisoformat(a["start_date"].replace("Z", "+00:00")).astimezone(WIB) >= week_start
        and datetime.fromisoformat(a["start_date"].replace("Z", "+00:00")).astimezone(WIB) <= display_end
        and (a.get("sport_type") or a.get("type", "")) in RUN_SPORTS
    ]
    week_gym = [
        a for a in activities
        if datetime.fromisoformat(a["start_date"].replace("Z", "+00:00")).astimezone(WIB) >= week_start
        and datetime.fromisoformat(a["start_date"].replace("Z", "+00:00")).astimezone(WIB) <= display_end
        and (a.get("sport_type") or a.get("type", "")) in {"WeightTraining", "Workout"}
    ]

    run_km = sum(a.get("distance", 0) for a in week_runs) / 1000
    run_cals = sum(a.get("kilojoules", 0) or 0 for a in week_runs) / 4.184
    run_cals += sum(a.get("calories", 0) or 0 for a in week_runs if not a.get("kilojoules"))
    gym_sessions = len(week_gym)

    days_elapsed = (display_end - week_start).days + 1

    # Weight data
    current_weight = db.get_current_weight()
    weight_trend = db.get_weight_trend(days=30)
    weight_stale_msg, weight_days_old = _weight_staleness(weight_trend)

    # Build embed
    week_label = "{} – {}".format(week_start.strftime("%-d %b"), display_end.strftime("%-d %b"))
    title = "🍽️ Nutrition Weekly Review — {}".format(week_label)
    if is_current_week:
        title += " (in progress)"

    desc = "Nutrisi + olahraga · {} hari".format(days_elapsed)
    if is_current_week:
        desc += " (minggu berjalan)"
    if current_weight:
        desc += " · ⚖️ {:.1f} kg".format(current_weight)
    embed = discord.Embed(title=title, description=desc, color=0xFF9800)

    # Weight section
    if len(weight_trend) >= 2:
        first_w = weight_trend[0]
        last_w = weight_trend[-1]
        delta = last_w["kg"] - first_w["kg"]
        sign = "+" if delta >= 0 else ""
        weight_lines = [
            "**Sekarang:** {:.1f} kg".format(last_w["kg"]),
            "**30 hari:** {}{:.1f} kg ({:.1f} → {:.1f})".format(sign, delta, first_w["kg"], last_w["kg"]),
        ]
        if weight_stale_msg:
            weight_lines.append(weight_stale_msg)
        embed.add_field(name="⚖️ Berat Badan", value="\n".join(weight_lines), inline=False)
    elif current_weight:
        val = "**{:.1f} kg** (belum cukup data trend)".format(current_weight)
        if weight_stale_msg:
            val += "\n" + weight_stale_msg
        embed.add_field(name="⚖️ Berat Badan", value=val, inline=False)
    else:
        embed.add_field(name="⚖️ Berat Badan", value=weight_stale_msg or "Belum ada data", inline=False)

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
        nut_lines.append("**Rata-rata: {} kcal/hari · P {}g**".format(avg_calories, avg_protein))

        if is_current_week and days_with_logs < days_elapsed:
            missing = days_elapsed - days_with_logs
            nut_lines.append("⚠️ {} hari tanpa food log".format(missing))

        embed.add_field(name="🍽️ Nutrisi", value="\n".join(nut_lines), inline=False)
    else:
        embed.add_field(name="🍽️ Nutrisi", value="Belum ada food log minggu ini", inline=False)

    # Exercise section
    exercise_lines = [
        "🏃 {} run · {:.1f} km".format(len(week_runs), run_km),
        "💪 {} gym session".format(gym_sessions),
    ]
    if run_cals > 0:
        exercise_lines.append("🔥 ~{:.0f} kcal burned (running)".format(run_cals))
    embed.add_field(name="🏋️ Olahraga", value="\n".join(exercise_lines), inline=False)

    # Build Claude prompt
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
        "- Running: {} runs, {:.1f} km, ~{:.0f} kcal burned\n"
        "- Gym: {} sessions"
    ).format(len(week_runs), run_km, run_cals, gym_sessions)

    weight_summary = ""
    if weight_trend:
        wt_lines = ["- {}: {:.1f} kg".format(w["date"], w["kg"]) for w in weight_trend]
        weight_summary = "\n".join(wt_lines)
        if len(weight_trend) >= 2:
            delta = weight_trend[-1]["kg"] - weight_trend[0]["kg"]
            weight_summary += "\nTrend 30 hari: {}{:.1f} kg".format("+" if delta >= 0 else "", delta)
        if weight_days_old > 3:
            weight_summary += "\n⚠️ Data berat terakhir {} hari lalu, mungkin tidak akurat".format(weight_days_old)
    elif current_weight:
        weight_summary = "Berat saat ini: {:.1f} kg (belum ada history)".format(current_weight)

    week_context = ""
    if is_current_week:
        days_left = 6 - now.weekday()
        week_context = (
            "PENTING: Ini adalah minggu yang SEDANG BERJALAN (hari ke-{} dari 7, sisa {} hari). "
            "Jangan evaluasi seolah minggu sudah selesai. "
            "Rekomendasi harus untuk SISA MINGGU INI, bukan minggu depan. "
            "Hari tanpa food log belum tentu skip — mungkin belum terjadi."
        ).format(days_elapsed, days_left)
    else:
        week_context = (
            "Ini adalah review untuk minggu yang SUDAH SELESAI. "
            "Evaluasi secara menyeluruh dan rekomendasi untuk minggu depan."
        )

    prompt = (
        "Kamu adalah personal coach untuk weight loss yang juga paham olahraga. "
        "Analisa data nutrisi + olahraga + berat badan secara holistik. Bahasa Indonesia, lo/gue.\n\n"
        "{}\n\n"
        "## Athlete Profile\n{}\n\n"
        "## Goals\n{}\n\n"
        "## Berat Badan (30 hari terakhir)\n{}\n\n"
        "## Nutrisi ({} hari dengan log)\n{}\n"
        "Rata-rata: {} kcal/hari, protein {} g/hari\n\n"
        "## Olahraga\n{}\n\n"
        "Berikan analisa dalam format:\n\n"
        "**Evaluasi:**\n"
        "Apakah intake kalori mendukung target weight loss? "
        "Apakah protein cukup (target 1.6g/kg)? Apakah deficit terlalu besar atau kurang? "
        "Bagaimana nutrisi terhadap beban latihan? "
        "Bagaimana trend berat badan? Apakah data berat up to date?\n\n"
        "**Terus lakukan:**\n- (2-3 hal spesifik berdasarkan data)\n\n"
        "**Stop/kurangi:**\n- (1-2 hal spesifik berdasarkan data)\n\n"
        "**Mulai lakukan:**\n- (2-3 hal spesifik berdasarkan data)\n\n"
        "Reference angka aktual dari data. Jangan generik. Maksimal 400 kata.\n\n"
        "PENTING FORMAT:\n"
        "- Jangan tulis heading (# atau ##)\n"
        "- Jangan pakai tabel markdown (| kolom |)\n"
        "- Gunakan **bold** untuk label, bullet list untuk daftar"
    ).format(
        week_context,
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
            max_tokens=900,
            messages=[{"role": "user", "content": prompt}],
        )
        insight = "".join(b.text for b in response.content if b.type == "text").strip()
    except Exception as e:
        print("Nutrition weekly review insight failed: {}".format(e))
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

    footer = "Nutrisi + Strava"
    if is_current_week:
        footer += " · minggu berjalan"
    else:
        footer += " · minggu selesai"
    embed.set_footer(text=footer)

    return embed, insight
