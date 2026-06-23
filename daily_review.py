"""Daily food intake review — recap macros from food_log entries."""

from datetime import datetime, timedelta, timezone

import anthropic
import discord

try:
    import supabase_client as supa
except ImportError:
    supa = None

WIB = timezone(timedelta(hours=7))
MODEL = "claude-sonnet-4-6"
CALORIE_TARGET_LOW = 1700
CALORIE_TARGET_HIGH = 2100


async def generate_daily_review(date_str: str = None, claude_client: anthropic.Anthropic = None) -> discord.Embed:
    """Build a daily food recap embed for the given date (or today)."""
    if date_str is None:
        date_str = datetime.now(WIB).strftime("%Y-%m-%d")

    entries = supa.get_food_for_date(date_str) if supa else []

    # Parse date for display
    day = datetime.strptime(date_str, "%Y-%m-%d")
    day_label = day.strftime("%-d %b %Y")

    if not entries:
        embed = discord.Embed(
            title=f"Daily Recap — {day_label}",
            description="Belum ada food log hari ini.",
            color=0x95A5A6,
        )
        embed.set_footer(text="📋 daily recap · fitness-coach")
        return embed

    # Aggregate macros
    total_cal = 0
    total_protein = 0.0
    total_fat = 0.0
    total_carbs = 0.0
    total_sugar = 0.0
    total_fiber = 0.0
    items = []

    for e in entries:
        cal = e.get("calories") or 0
        pro = float(e.get("protein") or 0)
        fat = float(e.get("fat") or 0)
        carb = float(e.get("carbs") or 0)
        sug = float(e.get("sugar") or 0)
        fib = float(e.get("fiber") or 0)

        total_cal += cal
        total_protein += pro
        total_fat += fat
        total_carbs += carb
        total_sugar += sug
        total_fiber += fib

        logged_at = e.get("logged_at", "")
        time_str = ""
        if logged_at:
            try:
                t = datetime.fromisoformat(logged_at).astimezone(WIB)
                time_str = t.strftime("%H:%M")
            except Exception:
                pass

        name = e.get("name", "?")
        items.append(f"**{time_str}** — {name} ({cal} kkal, {pro:.0f}g P)")

    embed = discord.Embed(
        title=f"Daily Recap — {day_label}",
        description=f"{len(entries)} item logged",
        color=0x2ECC71,
    )

    # Total macros
    macro_lines = [
        f"**Kalori:** {total_cal} kkal",
        f"**Protein:** {total_protein:.0f}g",
        f"**Lemak:** {total_fat:.0f}g",
        f"**Karbo:** {total_carbs:.0f}g",
    ]
    if total_sugar > 0:
        macro_lines.append(f"**Gula:** {total_sugar:.0f}g")
    if total_fiber > 0:
        macro_lines.append(f"**Serat:** {total_fiber:.0f}g")

    embed.add_field(name="Total Macros", value="\n".join(macro_lines), inline=False)

    # Item list
    if items:
        items_text = "\n".join(items)
        if len(items_text) > 1024:
            items_text = items_text[:1020] + "…"
        embed.add_field(name="Items", value=items_text, inline=False)

    # Goal checks
    goal_lines = []
    now_wib = datetime.now(WIB)
    hour = now_wib.hour
    is_reviewing_today = date_str == now_wib.strftime("%Y-%m-%d")

    # 1. Protein vs target (1.5x bodyweight)
    current_weight = None
    if supa:
        try:
            current_weight = supa.get_latest_weight()
        except Exception:
            pass
    bw = current_weight or 78
    protein_target = bw * 1.5
    protein_pct = round(total_protein / protein_target * 100) if protein_target else 0

    if is_reviewing_today and hour < 18:
        goal_lines.append(f"🔄 **Protein:** {total_protein:.0f}g / {protein_target:.0f}g ({protein_pct}%) — hari masih jalan")
    elif protein_pct >= 100:
        goal_lines.append(f"✅ **Protein:** {total_protein:.0f}g / {protein_target:.0f}g — terpenuhi!")
    elif protein_pct >= 60:
        gap = protein_target - total_protein
        goal_lines.append(f"⚠️ **Protein:** {total_protein:.0f}g / {protein_target:.0f}g ({protein_pct}%) — kurang {gap:.0f}g")
    else:
        goal_lines.append(f"❌ **Protein:** {total_protein:.0f}g / {protein_target:.0f}g ({protein_pct}%) — jauh di bawah target")

    # 2. Calorie check (deficit target 1700-2100)
    if is_reviewing_today and hour < 18:
        goal_lines.append(f"🔄 **Kalori:** {total_cal} kkal — target akhir hari {CALORIE_TARGET_LOW}–{CALORIE_TARGET_HIGH} kkal")
    elif total_cal < CALORIE_TARGET_LOW - 200:
        goal_lines.append(f"❌ **Kalori:** {total_cal} kkal — terlalu rendah (<{CALORIE_TARGET_LOW}), risiko muscle loss")
    elif total_cal <= CALORIE_TARGET_HIGH:
        goal_lines.append(f"✅ **Kalori:** {total_cal} kkal — dalam range deficit ({CALORIE_TARGET_LOW}–{CALORIE_TARGET_HIGH})")
    else:
        surplus = total_cal - CALORIE_TARGET_HIGH
        goal_lines.append(f"⚠️ **Kalori:** {total_cal} kkal — {surplus} kkal di atas target deficit")

    # 3. Weight progress
    if current_weight:
        target_weight = 74.0
        remaining = current_weight - target_weight
        if remaining <= 0:
            goal_lines.append(f"🎯 **Berat:** {current_weight:.1f} kg — TARGET TERCAPAI!")
        else:
            goal_lines.append(f"⚖️ **Berat:** {current_weight:.1f} kg → {target_weight:.0f} kg (sisa {remaining:.1f} kg)")

    if goal_lines:
        embed.add_field(name="Goal Check", value="\n".join(goal_lines), inline=False)

    # Smart analysis from Claude
    if claude_client and entries:
        try:
            food_list = ", ".join(e.get("name", "?") for e in entries)
            time_context = "tengah hari, belum semua meal ke-log" if (is_reviewing_today and hour < 18) else "akhir hari / recap final"

            analysis_prompt = (
                "Kamu coach nutrisi. Analisis singkat (2-3 kalimat, bahasa lo/gue casual) soal pilihan makan hari ini.\n\n"
                f"Waktu: {time_context}\n"
                f"Food items: {food_list}\n"
                f"Total: {total_cal} kkal, protein {total_protein:.0f}g, lemak {total_fat:.0f}g, karbo {total_carbs:.0f}g\n"
                f"Target: protein ≥{protein_target:.0f}g/hari, kalori {CALORIE_TARGET_LOW}-{CALORIE_TARGET_HIGH} kkal (deficit)\n"
                f"Berat sekarang: {bw} kg, target 74 kg\n\n"
                "PENTING: kalau masih tengah hari, jangan conclude — bilang on track / perlu adjust sisa hari. "
                "Jangan pakai heading (#). Jangan generik. Reference makanan spesifik yang di-log."
            )
            resp = claude_client.messages.create(
                model=MODEL,
                max_tokens=200,
                messages=[{"role": "user", "content": analysis_prompt}],
            )
            analysis = "".join(b.text for b in resp.content if b.type == "text").strip()
            if analysis:
                embed.add_field(name="Analisis", value=analysis[:1024], inline=False)
        except Exception as e:
            print(f"Daily review analysis failed: {e}")

    embed.set_footer(text=f"📋 daily recap · {len(entries)} entries · fitness-coach")

    return embed
