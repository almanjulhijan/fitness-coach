"""Data enrichment for post-run analysis.

Fetches weather, AQI, and location context from external APIs,
and computes derived metrics from Strava activity data.
"""

import asyncio
import os
from datetime import datetime, timedelta, timezone

import aiohttp

WIB = timezone(timedelta(hours=7))

RUN_SPORTS = {"Run", "TrailRun", "VirtualRun"}


def _read_max_hr_from_kb(default: int = 185) -> int:
    """Read Max HR from knowledge_base/about_me.md. Falls back to default."""
    import re
    kb_path = os.path.join(os.path.dirname(__file__), "knowledge_base", "about_me.md")
    try:
        text = open(kb_path, encoding="utf-8").read()
        m = re.search(r"\*\*Max HR:\*\*\s*(\d+)", text)
        if m:
            return int(m.group(1))
    except Exception:
        pass
    return default

# ── External API helpers ────────────────────────────────────────────────────────

async def get_weather(lat: float, lon: float, timestamp: datetime, api_key: str = "") -> dict:
    """Fetch historical weather from Open-Meteo (free, no API key required).

    Returns a dict with: temp_c, feels_like_c, humidity_pct, wind_kmh.
    Returns {} on failure.
    """
    if lat is None or lon is None:
        return {}

    date_str = timestamp.strftime("%Y-%m-%d")
    url = "https://archive-api.open-meteo.com/v1/archive"
    params = {
        "latitude": lat,
        "longitude": lon,
        "start_date": date_str,
        "end_date": date_str,
        "hourly": "temperature_2m,apparent_temperature,relativehumidity_2m,windspeed_10m",
        "timezone": "Asia/Jakarta",
        "wind_speed_unit": "kmh",
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status != 200:
                    return {}
                data = await r.json()

        hourly = data.get("hourly", {})
        times = hourly.get("time", [])
        if not times:
            return {}

        local_hour = timestamp.astimezone(WIB).strftime("%Y-%m-%dT%H:00")
        idx = next(
            (i for i, t in enumerate(times) if t == local_hour),
            min(range(len(times)), key=lambda i: abs(
                datetime.fromisoformat(times[i]).hour - timestamp.astimezone(WIB).hour
            ))
        )

        return {
            "temp_c": round(hourly["temperature_2m"][idx], 1),
            "feels_like_c": round(hourly["apparent_temperature"][idx], 1),
            "humidity_pct": int(hourly["relativehumidity_2m"][idx]),
            "wind_kmh": round(hourly["windspeed_10m"][idx], 1),
        }
    except Exception as e:
        print("Weather fetch failed: {}".format(e))
        return {}


async def get_aqi(lat: float, lon: float, api_key: str) -> dict:
    """Fetch AQI from WAQI (World Air Quality Index) for the nearest station.

    Returns a dict with: aqi (int), level (str), dominant_pollutant (str).
    Returns {} on failure.
    """
    if not api_key or lat is None or lon is None:
        return {}

    url = "https://api.waqi.info/feed/geo:{};{}/".format(lat, lon)
    params = {"token": api_key}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status != 200:
                    return {}
                data = await r.json()
                if data.get("status") != "ok":
                    return {}
                aqi_val = data["data"].get("aqi")
                if aqi_val is None or not isinstance(aqi_val, (int, float)):
                    return {}
                return {
                    "aqi": int(aqi_val),
                    "level": _aqi_level(int(aqi_val)),
                    "dominant_pollutant": data["data"].get("dominentpol", ""),
                }
    except Exception as e:
        print("AQI fetch failed: {}".format(e))
        return {}


def _aqi_level(aqi: int) -> str:
    if aqi <= 50:
        return "Good"
    if aqi <= 100:
        return "Moderate"
    if aqi <= 150:
        return "Unhealthy for sensitive groups"
    if aqi <= 200:
        return "Unhealthy"
    if aqi <= 300:
        return "Very Unhealthy"
    return "Hazardous"


async def get_location_name(lat: float, lon: float) -> str:
    """Reverse geocode via OpenStreetMap Nominatim (no API key needed).

    Returns a short readable name like "Summarecon, Bekasi".
    Returns "" on failure.
    """
    if lat is None or lon is None:
        return ""

    url = "https://nominatim.openstreetmap.org/reverse"
    params = {"lat": lat, "lon": lon, "format": "json", "zoom": 14}
    headers = {"User-Agent": "fitness-coach-bot/1.0"}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status != 200:
                    return ""
                data = await r.json()
                addr = data.get("address", {})
                parts = []
                for key in ("suburb", "neighbourhood", "city_district", "town", "city"):
                    val = addr.get(key)
                    if val and val not in parts:
                        parts.append(val)
                    if len(parts) == 2:
                        break
                return ", ".join(parts) if parts else data.get("display_name", "").split(",")[0]
    except Exception as e:
        print("Reverse geocode failed: {}".format(e))
        return ""


def get_time_context(start_time: datetime, lat: float, lon: float) -> str:
    """Return a human-readable time-of-day label relative to sunrise/sunset.

    Tries to use astral if installed; falls back to clock-based heuristics.
    """
    try:
        from astral import LocationInfo
        from astral.sun import sun

        loc = LocationInfo(latitude=lat, longitude=lon, timezone="Asia/Jakarta")
        s = sun(loc.observer, date=start_time.date(), tzinfo=WIB)
        sunrise = s["sunrise"]
        sunset = s["sunset"]

        local = start_time.astimezone(WIB)
        if local < sunrise - timedelta(minutes=30):
            return "before sunrise"
        if local < sunrise + timedelta(minutes=30):
            return "sunrise run"
        if local < sunrise + timedelta(hours=2):
            return "morning run"
        if local < datetime.combine(local.date(), datetime.min.time()).replace(tzinfo=WIB) + timedelta(hours=12):
            return "late morning"
        if local < sunset - timedelta(hours=1):
            return "afternoon run"
        if local < sunset + timedelta(minutes=30):
            return "sunset run"
        return "evening run"
    except Exception:
        hour = start_time.astimezone(WIB).hour
        if hour < 6:
            return "before sunrise"
        if hour < 9:
            return "morning run"
        if hour < 12:
            return "late morning"
        if hour < 15:
            return "afternoon run"
        if hour < 18:
            return "evening run"
        return "night run"


# ── Computed metrics from Strava data ──────────────────────────────────────────

def compute_aerobic_decoupling(activity: dict) -> float | None:
    """Compute aerobic decoupling from per-km splits.

    Aerobic decoupling = drift in HR:pace efficiency between first and second half.
    < 5% = good aerobic base. > 10% = significant cardiac drift.
    Returns None if splits data is insufficient.
    """
    splits = activity.get("splits_metric") or []
    if len(splits) < 4:
        return None

    def efficiency(splits_subset):
        total_dist = sum(s.get("distance", 0) for s in splits_subset)
        total_time = sum(s.get("moving_time", 0) for s in splits_subset)
        hr_values = [s["average_heartrate"] for s in splits_subset if s.get("average_heartrate")]
        if not hr_values or not total_time or not total_dist:
            return None
        avg_speed = total_dist / total_time  # m/s
        avg_hr = sum(hr_values) / len(hr_values)
        return avg_speed / avg_hr

    mid = len(splits) // 2
    ef1 = efficiency(splits[:mid])
    ef2 = efficiency(splits[mid:])

    if ef1 is None or ef2 is None or ef1 == 0:
        return None

    decoupling = abs((ef1 - ef2) / ef1) * 100
    return round(decoupling, 1)


def compute_hr_zones(activity: dict, max_hr: int = 185) -> dict:
    """Estimate HR zone distribution from splits.

    Uses 5-zone model based on % of max HR.
    Returns {zone_label: percentage} or {} if no HR data.
    """
    splits = activity.get("splits_metric") or []
    hr_values = [s["average_heartrate"] for s in splits if s.get("average_heartrate")]
    if not hr_values:
        avg_hr = activity.get("average_heartrate")
        if not avg_hr:
            return {}
        hr_values = [avg_hr]

    zones = {"Zone 1": 0, "Zone 2": 0, "Zone 3": 0, "Zone 4": 0, "Zone 5": 0}
    for hr in hr_values:
        pct = hr / max_hr * 100
        if pct < 60:
            zones["Zone 1"] += 1
        elif pct < 70:
            zones["Zone 2"] += 1
        elif pct < 80:
            zones["Zone 3"] += 1
        elif pct < 90:
            zones["Zone 4"] += 1
        else:
            zones["Zone 5"] += 1

    total = len(hr_values)
    result = {}
    for zone, count in zones.items():
        pct = round(count / total * 100)
        if pct > 0:
            result[zone] = pct
    return result


def compute_training_load(activities: list[dict]) -> dict:
    """Compare volume (km) this week vs last week.

    Returns: {this_week_km, last_week_km, change_pct, this_week_runs, last_week_runs}
    """
    now = datetime.now(timezone.utc)
    week_start = now - timedelta(days=now.weekday() + 1)
    prev_week_start = week_start - timedelta(days=7)

    this_week = [
        a for a in activities
        if datetime.fromisoformat(a["start_date"].replace("Z", "+00:00")) >= week_start
        and (a.get("sport_type") or a.get("type")) in RUN_SPORTS
    ]
    last_week = [
        a for a in activities
        if prev_week_start <= datetime.fromisoformat(a["start_date"].replace("Z", "+00:00")) < week_start
        and (a.get("sport_type") or a.get("type")) in RUN_SPORTS
    ]

    tw_km = sum(a.get("distance", 0) for a in this_week) / 1000
    lw_km = sum(a.get("distance", 0) for a in last_week) / 1000
    change_pct = round((tw_km - lw_km) / lw_km * 100) if lw_km else None
    days_into_week = (now - week_start).days

    return {
        "this_week_km": round(tw_km, 1),
        "last_week_km": round(lw_km, 1),
        "change_pct": change_pct,
        "this_week_runs": len(this_week),
        "last_week_runs": len(last_week),
        "days_into_week": days_into_week,
    }


def detect_milestones(activity: dict, activities: list[dict]) -> list[str]:
    """Detect noteworthy achievements for this activity compared to recent history.

    Returns a list of milestone strings (empty list if none).
    """
    sport = activity.get("sport_type") or activity.get("type", "")
    if sport not in RUN_SPORTS:
        return []

    dist_m = activity.get("distance", 0)
    moving = activity.get("moving_time", 0)
    milestones = []

    run_history = [
        a for a in activities
        if (a.get("sport_type") or a.get("type")) in RUN_SPORTS
        and a.get("id") != activity.get("id")
    ]

    if run_history:
        if dist_m and all(a.get("distance", 0) <= dist_m for a in run_history):
            milestones.append("Longest run in the last 30 days — {:.1f} km".format(dist_m / 1000))

        if dist_m and moving:
            pace = moving / (dist_m / 1000)
            faster = [
                a for a in run_history
                if a.get("distance", 0) >= dist_m * 0.9
                and a.get("moving_time") and a.get("distance")
                and a["moving_time"] / (a["distance"] / 1000) > pace
            ]
            if len(faster) == len([a for a in run_history if a.get("distance", 0) >= dist_m * 0.9]):
                m, s = divmod(int(pace), 60)
                milestones.append("Best pace this month for this distance — {}:{:02d}/km".format(m, s))

    run_streak = _compute_run_streak(activities)
    if run_streak >= 3:
        milestones.append("{} day running streak".format(run_streak))

    return milestones


def _compute_run_streak(activities: list[dict]) -> int:
    """Count consecutive days with at least one run (up to and including today)."""
    run_dates = sorted({
        datetime.fromisoformat(a["start_date"].replace("Z", "+00:00"))
        .astimezone(WIB).date()
        for a in activities
        if (a.get("sport_type") or a.get("type")) in RUN_SPORTS
    }, reverse=True)

    if not run_dates:
        return 0

    today = datetime.now(WIB).date()
    if run_dates[0] < today - timedelta(days=1):
        return 0

    streak = 1
    for i in range(1, len(run_dates)):
        if run_dates[i - 1] - run_dates[i] == timedelta(days=1):
            streak += 1
        else:
            break
    return streak


def heat_adjusted_pace(pace_sec_km: float, temp_c: float, humidity_pct: float) -> float:
    """Estimate equivalent pace in ideal conditions (16°C, 40% humidity).

    Uses a simplified heat/humidity correction factor.
    Formula adapted from research by McArdle, Katch & Katch.
    Returns adjusted pace in sec/km.
    """
    if not pace_sec_km:
        return pace_sec_km

    heat_index = temp_c + 0.33 * (humidity_pct / 100 * 6.105 * (1 - (temp_c - 14.55) / 100)) - 4.0
    baseline_hi = 16.0

    correction = max(0.0, (heat_index - baseline_hi) * 0.005)
    adjusted = pace_sec_km * (1 - correction)
    return max(adjusted, pace_sec_km * 0.85)


# ── Main enrichment entry point ─────────────────────────────────────────────────

async def enrich_activity(activity: dict, activities: list[dict]) -> dict:
    """Gather all enrichment data for a single activity.

    Returns a dict with all enriched fields ready for post_run.py to consume.
    """
    aqi_key = os.getenv("WAQI_API_KEY", "")
    max_hr = _read_max_hr_from_kb()

    start_latlng = activity.get("start_latlng") or []
    lat = start_latlng[0] if len(start_latlng) >= 2 else None
    lon = start_latlng[1] if len(start_latlng) >= 2 else None

    start_time = datetime.fromisoformat(
        activity["start_date"].replace("Z", "+00:00")
    )

    weather, aqi_data, location_name = await asyncio.gather(
        get_weather(lat, lon, start_time),
        get_aqi(lat, lon, aqi_key),
        get_location_name(lat, lon),
    )

    time_context = get_time_context(start_time, lat, lon) if lat else ""
    decoupling = compute_aerobic_decoupling(activity)
    hr_zones = compute_hr_zones(activity, max_hr=max_hr)
    training_load = compute_training_load(activities)
    milestones = detect_milestones(activity, activities)

    pace_sec_km = None
    dist_m = activity.get("distance", 0)
    moving = activity.get("moving_time", 0)
    if dist_m and moving:
        pace_sec_km = moving / (dist_m / 1000)

    adjusted_pace_sec_km = None
    if pace_sec_km and weather.get("temp_c") and weather.get("humidity_pct"):
        adjusted_pace_sec_km = heat_adjusted_pace(
            pace_sec_km, weather["temp_c"], weather["humidity_pct"]
        )

    avg_hr = activity.get("average_heartrate")
    hr_baseline = None
    if avg_hr:
        recent_runs = [
            a for a in activities
            if (a.get("sport_type") or a.get("type")) in RUN_SPORTS
            and a.get("average_heartrate")
            and a.get("id") != activity.get("id")
        ]
        if len(recent_runs) >= 3:
            hr_baseline = round(
                sum(a["average_heartrate"] for a in recent_runs[:10]) / min(len(recent_runs), 10)
            )

    return {
        "weather": weather,
        "aqi": aqi_data,
        "location_name": location_name,
        "time_context": time_context,
        "aerobic_decoupling": decoupling,
        "hr_zones": hr_zones,
        "training_load": training_load,
        "milestones": milestones,
        "pace_sec_km": pace_sec_km,
        "adjusted_pace_sec_km": adjusted_pace_sec_km,
        "hr_baseline": hr_baseline,
    }
