"""Thin wrapper around Supabase for weight, activity, and snapshot storage."""

import os
from datetime import datetime, timedelta, timezone
from typing import Optional

try:
    from supabase import create_client, Client
except ImportError:
    create_client = None
    Client = None

WIB = timezone(timedelta(hours=7))

_client: Optional[Client] = None


def get_supabase() -> Optional[Client]:
    global _client
    if _client is not None:
        return _client
    url = os.getenv("SUPABASE_URL", "").strip()
    key = os.getenv("SUPABASE_KEY", "").strip()
    if not url or not key:
        return None
    _client = create_client(url, key)
    return _client


# ── Weight ────────────────────────────────────────────────────────────────────


def log_weight(weight_kg: float, source: str = "manual", note: str | None = None) -> bool:
    sb = get_supabase()
    if not sb:
        return False
    row = {"weight_kg": weight_kg, "source": source}
    if note:
        row["note"] = note
    sb.table("weight_log").insert(row).execute()
    return True


def get_latest_weight() -> float | None:
    sb = get_supabase()
    if not sb:
        return None
    resp = sb.table("weight_log").select("weight_kg").order("logged_at", desc=True).limit(1).execute()
    if resp.data:
        return float(resp.data[0]["weight_kg"])
    return None


def get_weight_history(days: int = 90) -> list[dict]:
    sb = get_supabase()
    if not sb:
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    resp = (
        sb.table("weight_log")
        .select("weight_kg, source, logged_at, note")
        .gte("logged_at", cutoff)
        .order("logged_at", desc=True)
        .execute()
    )
    return resp.data or []


def get_weight_trend(weeks: int = 4) -> dict:
    """Return current weight, previous week's weight, and change."""
    history = get_weight_history(days=weeks * 7 + 7)
    if not history:
        return {"current": None, "prev_week": None, "change_kg": None}

    current = float(history[0]["weight_kg"])

    one_week_ago = datetime.now(timezone.utc) - timedelta(days=7)
    older = [
        h for h in history
        if datetime.fromisoformat(h["logged_at"]) < one_week_ago
    ]
    prev_week = float(older[0]["weight_kg"]) if older else None

    change_kg = round(current - prev_week, 2) if prev_week is not None else None
    return {"current": current, "prev_week": prev_week, "change_kg": change_kg}


# ── Activities ────────────────────────────────────────────────────────────────


def upsert_activity(activity: dict) -> bool:
    sb = get_supabase()
    if not sb:
        return False
    row = {
        "strava_id": activity["id"],
        "sport_type": activity.get("sport_type") or activity.get("type", "Unknown"),
        "name": activity.get("name"),
        "distance_m": activity.get("distance"),
        "moving_time_s": activity.get("moving_time"),
        "elapsed_time_s": activity.get("elapsed_time"),
        "total_elevation_gain": activity.get("total_elevation_gain"),
        "average_heartrate": activity.get("average_heartrate"),
        "max_heartrate": activity.get("max_heartrate"),
        "average_speed": activity.get("average_speed"),
        "start_date": activity.get("start_date"),
        "suffer_score": activity.get("suffer_score"),
        "raw_json": activity,
    }
    sb.table("activities").upsert(row, on_conflict="strava_id").execute()
    return True


def upsert_activities(activities: list[dict]) -> int:
    count = 0
    for act in activities:
        if upsert_activity(act):
            count += 1
    return count


# ── Weekly Snapshots ──────────────────────────────────────────────────────────


def save_weekly_snapshot(data: dict) -> bool:
    sb = get_supabase()
    if not sb:
        return False
    sb.table("weekly_snapshots").upsert(data, on_conflict="week_start").execute()
    return True


def get_weekly_snapshots(weeks: int = 8) -> list[dict]:
    sb = get_supabase()
    if not sb:
        return []
    cutoff = (datetime.now(WIB) - timedelta(weeks=weeks)).strftime("%Y-%m-%d")
    resp = (
        sb.table("weekly_snapshots")
        .select("*")
        .gte("week_start", cutoff)
        .order("week_start", desc=True)
        .execute()
    )
    return resp.data or []
