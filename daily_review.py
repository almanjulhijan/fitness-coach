"""Daily food intake review — recap macros from food_log entries."""

from datetime import datetime, timedelta, timezone

import discord

try:
    import supabase_client as supa
except ImportError:
    supa = None

WIB = timezone(timedelta(hours=7))


async def generate_daily_review(date_str: str = None) -> discord.Embed:
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

    # 1. Protein vs target (1.5x bodyweight)
    current_weight = None
    if supa:
        try:
            current_weight = supa.get_latest_weight()
        except Exception:
            pass
    bw = current_weight or 78
    protein_target = bw * 1.5
    pct = round(total_protein / protein_target * 100) if protein_target else 0
    if pct >= 100:
        goal_lines.append(f"✅ **Protein:** {total_protein:.0f}g / {protein_target:.0f}g target — terpenuhi!")
    elif pct >= 60:
        goal_lines.append(f"⚠️ **Protein:** {total_protein:.0f}g / {protein_target:.0f}g target ({pct}%) — kurang {protein_target - total_protein:.0f}g lagi")
    else:
        goal_lines.append(f"❌ **Protein:** {total_protein:.0f}g / {protein_target:.0f}g target ({pct}%) — jauh di bawah target")

    # 2. Weight progress (if data available)
    if current_weight:
        target_weight = 74.0
        remaining = current_weight - target_weight
        if remaining <= 0:
            goal_lines.append(f"🎯 **Berat:** {current_weight:.1f} kg — TARGET TERCAPAI!")
        else:
            goal_lines.append(f"⚖️ **Berat:** {current_weight:.1f} kg → target {target_weight:.0f} kg (sisa {remaining:.1f} kg)")

    if goal_lines:
        embed.add_field(name="Goal Check", value="\n".join(goal_lines), inline=False)

    embed.set_footer(text=f"📋 daily recap · {len(entries)} entries · fitness-coach")

    return embed
