"""Food logging — analyze food photos/text and return structured Discord embed."""

import json
import anthropic
import discord

try:
    import supabase_client as supa
except ImportError:
    supa = None

MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 512


async def analyze_food(user_content, claude_client: anthropic.Anthropic) -> discord.Embed:
    """Analyze food from image/text and return a structured Discord embed."""
    prompt_block = {
        "type": "text",
        "text": (
            "Kamu adalah food nutrition analyst. Analisis makanan/minuman dari gambar dan/atau teks ini.\n\n"
            "PENTING: Respond HANYA dalam JSON format berikut, tanpa teks lain:\n"
            '{"name": "nama makanan/minuman", "portion": "deskripsi porsi", '
            '"calories": 123, "protein": 12, "fat": 5, "carbs": 20, "sugar": 8, '
            '"fiber": 2, "verdict": "satu kalimat penilaian singkat untuk athlete"}\n\n'
            "Semua angka dalam gram kecuali calories (kkal). "
            "Kalau ada nutrition label di foto, pakai data dari label. "
            "Kalau tidak ada label, estimasi berdasarkan porsi umum. "
            "Untuk verdict, fokus pada relevansi untuk runner/athlete (recovery, fuel, dll)."
        ),
    }

    if isinstance(user_content, list):
        messages_content = user_content[:]
        has_text = any(b.get("type") == "text" for b in messages_content)
        if has_text:
            for b in messages_content:
                if b.get("type") == "text":
                    b["text"] = b["text"] + "\n\n" + prompt_block["text"]
                    break
        else:
            messages_content.append(prompt_block)
    else:
        messages_content = [{"type": "text", "text": (user_content or "") + "\n\n" + prompt_block["text"]}]

    response = claude_client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        messages=[{"role": "user", "content": messages_content}],
    )

    raw = "".join(b.text for b in response.content if b.type == "text").strip()

    # Extract JSON from response (Claude might wrap in ```json blocks)
    if "```" in raw:
        raw = raw.split("```json")[-1].split("```")[0].strip() if "```json" in raw else raw.split("```")[1].split("```")[0].strip()

    data = json.loads(raw)

    embed = discord.Embed(
        title=data.get("name", "Food Log"),
        description=data.get("portion", ""),
        color=0x2ECC71,
    )

    cal = data.get("calories", "?")
    protein = data.get("protein", "?")
    fat = data.get("fat", "?")
    carbs = data.get("carbs", "?")
    sugar = data.get("sugar")
    fiber = data.get("fiber")

    macro_lines = [
        f"**Kalori:** {cal} kkal",
        f"**Protein:** {protein}g",
        f"**Lemak:** {fat}g",
        f"**Karbo:** {carbs}g",
    ]
    if sugar is not None:
        macro_lines.append(f"**Gula:** {sugar}g")
    if fiber is not None:
        macro_lines.append(f"**Serat:** {fiber}g")

    embed.add_field(name="Nutrisi", value="\n".join(macro_lines), inline=False)

    verdict = data.get("verdict", "")
    if verdict:
        embed.add_field(name="Verdict", value=verdict, inline=False)

    # Determine source type
    has_image = isinstance(user_content, list) and any(b.get("type") == "image" for b in user_content)
    has_text_input = isinstance(user_content, str) or (
        isinstance(user_content, list) and any(
            b.get("type") == "text" and b.get("text", "").strip() not in ("", "Analisis gambar ini.")
            for b in user_content
        )
    )
    if has_image and has_text_input:
        source = "combined"
    elif has_image:
        source = "photo"
    else:
        source = "text"

    # Save to Supabase
    if supa:
        try:
            supa.log_food({**data, "source": source})
        except Exception as e:
            print(f"Failed to save food log to Supabase: {e}")

    saved_label = " · ✅ logged" if supa and supa.get_supabase() else ""
    embed.set_footer(text=f"📸 food log · fitness-coach{saved_label}")

    return embed
