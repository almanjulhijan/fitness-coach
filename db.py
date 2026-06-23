"""Supabase storage layer for all persistent mutable data."""

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from supabase import create_client, Client

WIB = timezone(timedelta(hours=7))

_client: Optional[Client] = None


def get_client() -> Client:
    global _client
    if _client is None:
        url = os.getenv("SUPABASE_URL", "").strip()
        key = os.getenv("SUPABASE_KEY", "").strip()
        if not url or not key:
            raise RuntimeError("SUPABASE_URL and SUPABASE_KEY must be set")
        _client = create_client(url, key)
    return _client


# ── Athlete Profile ──────────────────────────────────────────────────────────

def get_profile_field(field: str) -> Optional[str]:
    sb = get_client()
    result = sb.table("athlete_profile").select("value").eq("field", field).execute()
    if result.data:
        return result.data[0]["value"]
    return None


def update_profile_field(field: str, value: str) -> None:
    sb = get_client()
    existing = sb.table("athlete_profile").select("id").eq("field", field).execute()
    if existing.data:
        sb.table("athlete_profile").update({
            "value": value,
            "updated_at": datetime.now(WIB).isoformat(),
        }).eq("field", field).execute()
    else:
        sb.table("athlete_profile").insert({
            "field": field,
            "value": value,
            "updated_at": datetime.now(WIB).isoformat(),
        }).execute()


def get_all_profile_fields() -> dict[str, str]:
    sb = get_client()
    result = sb.table("athlete_profile").select("field, value").execute()
    return {row["field"]: row["value"] for row in result.data}


# ── Weight Log ───────────────────────────────────────────────────────────────

def save_weight(kg: float, timestamp: datetime = None) -> dict:
    ts = timestamp or datetime.now(WIB)
    entry = {
        "kg": round(kg, 1),
        "logged_at": ts.isoformat(),
        "date": ts.strftime("%Y-%m-%d"),
    }
    sb = get_client()
    sb.table("weight_log").insert(entry).execute()
    update_profile_field("Weight", "{:.1f} kg".format(kg))
    return entry


def load_weight_history() -> list[dict]:
    sb = get_client()
    result = sb.table("weight_log").select("*").order("logged_at").execute()
    return result.data


def get_current_weight() -> Optional[float]:
    sb = get_client()
    result = (
        sb.table("weight_log")
        .select("kg")
        .order("logged_at", desc=True)
        .limit(1)
        .execute()
    )
    if result.data:
        return result.data[0]["kg"]
    return None


def get_weight_trend(days: int = 30) -> list[dict]:
    cutoff = (datetime.now(WIB) - timedelta(days=days)).strftime("%Y-%m-%d")
    sb = get_client()
    result = (
        sb.table("weight_log")
        .select("kg, date, logged_at")
        .gte("date", cutoff)
        .order("logged_at")
        .execute()
    )
    by_date = {}
    for row in result.data:
        by_date[row["date"]] = row
    return [by_date[d] for d in sorted(by_date.keys())]


# ── Food Entries ─────────────────────────────────────────────────────────────

def save_food_entry(entry: dict, timestamp: datetime = None) -> dict:
    ts = timestamp or datetime.now(WIB)
    row = {
        "food": entry.get("food", "unknown"),
        "calories": entry.get("calories", 0),
        "protein_g": entry.get("protein_g", 0),
        "carbs_g": entry.get("carbs_g", 0),
        "fat_g": entry.get("fat_g", 0),
        "notes": entry.get("notes", ""),
        "logged_at": ts.isoformat(),
        "date": ts.strftime("%Y-%m-%d"),
        "time_label": ts.strftime("%H:%M"),
    }
    sb = get_client()
    sb.table("food_entries").insert(row).execute()
    return {**entry, "timestamp": ts.isoformat(), "time_label": row["time_label"]}


def load_daily_entries(date: datetime = None) -> list[dict]:
    dt = date or datetime.now(WIB)
    date_str = dt.strftime("%Y-%m-%d")
    sb = get_client()
    result = (
        sb.table("food_entries")
        .select("*")
        .eq("date", date_str)
        .order("logged_at")
        .execute()
    )
    return result.data


def load_entries_range(start: datetime, end: datetime) -> dict[str, list[dict]]:
    start_str = start.strftime("%Y-%m-%d")
    end_str = end.strftime("%Y-%m-%d")
    sb = get_client()
    result = (
        sb.table("food_entries")
        .select("*")
        .gte("date", start_str)
        .lte("date", end_str)
        .order("logged_at")
        .execute()
    )
    grouped: dict[str, list[dict]] = {}
    for row in result.data:
        grouped.setdefault(row["date"], []).append(row)
    return grouped
