from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

STRAVA_API_BASE = "https://www.strava.com/api/v3"
WIB = timezone(timedelta(hours=7))  # Waktu Indonesia Barat (UTC+7)

# Sports where pace (min/km) makes sense
PACE_SPORTS = {"Run", "TrailRun", "VirtualRun", "Walk", "Hike"}
# Sports where speed (km/h) makes sense
SPEED_SPORTS = {"Ride", "VirtualRide", "MountainBikeRide", "GravelRide", "EBikeRide"}
# Swimming uses pace per 100m
SWIM_SPORTS = {"Swim"}


class StravaClient:
    def __init__(self, access_token: str):
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {access_token}"})

    def get_athlete(self) -> dict:
        resp = self.session.get(f"{STRAVA_API_BASE}/athlete", timeout=30)
        resp.raise_for_status()
        return resp.json()

    def get_athlete_stats(self, athlete_id: int) -> dict:
        resp = self.session.get(f"{STRAVA_API_BASE}/athletes/{athlete_id}/stats", timeout=30)
        resp.raise_for_status()
        return resp.json()

    def get_activities(self, days: int = 30, per_page: int = 50) -> list[dict]:
        after = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp())
        activities = []
        page = 1

        while True:
            resp = self.session.get(
                f"{STRAVA_API_BASE}/athlete/activities",
                params={"after": after, "per_page": per_page, "page": page},
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            if not data:
                break
            activities.extend(data)
            if len(data) < per_page:
                break
            page += 1

        return activities

    def get_activity(self, activity_id: int) -> dict:
        """Fetch a single activity with full detail (includes splits_metric, laps, etc.)."""
        resp = self.session.get(f"{STRAVA_API_BASE}/activities/{activity_id}", timeout=30)
        resp.raise_for_status()
        return resp.json()

    def format_activities_summary(self, activities: list[dict], athlete: Optional[dict] = None) -> str:
        header_parts = ["## Recent Training Data (Last 30 Days)"]

        if athlete:
            name = f"{athlete.get('firstname', '')} {athlete.get('lastname', '')}".strip()
            if name:
                header_parts.append(f"**Athlete:** {name}")

        if not activities:
            return "\n".join(header_parts) + "\n\nNo activities recorded in the last 30 days."

        header_parts.append(f"**Total activities:** {len(activities)}")

        # Aggregate stats per sport type
        sport_totals: dict[str, dict] = {}
        for act in activities:
            sport = act.get("sport_type") or act.get("type", "Unknown")
            if sport not in sport_totals:
                sport_totals[sport] = {"count": 0, "distance_m": 0, "time_s": 0}
            sport_totals[sport]["count"] += 1
            sport_totals[sport]["distance_m"] += act.get("distance", 0)
            sport_totals[sport]["time_s"] += act.get("elapsed_time", 0)

        summary_lines = ["\n".join(header_parts), ""]

        # Sport summary
        summary_lines.append("### Summary by Sport")
        for sport, totals in sorted(sport_totals.items()):
            dist_km = totals["distance_m"] / 1000
            hrs = totals["time_s"] // 3600
            mins = (totals["time_s"] % 3600) // 60
            time_str = f"{hrs}h {mins:02d}m" if hrs else f"{mins}m"
            dist_str = f"{dist_km:.1f} km" if dist_km else "—"
            summary_lines.append(
                f"- **{sport}**: {totals['count']} sessions, {dist_str}, {time_str} total"
            )

        # Individual activities
        summary_lines.append("\n### Activity Log")
        for act in sorted(activities, key=lambda a: a["start_date"], reverse=True):
            line = _format_single_activity(act)
            summary_lines.append(line)

        return "\n".join(summary_lines)


def _format_single_activity(act: dict) -> str:
    date = datetime.fromisoformat(act["start_date"].replace("Z", "+00:00")).astimezone(WIB)
    date_str = date.strftime("%Y-%m-%d %H:%M WIB")

    sport = act.get("sport_type") or act.get("type", "Unknown")
    name = act.get("name", "Untitled")
    dist_m = act.get("distance", 0)
    elapsed = act.get("elapsed_time", 0)
    moving = act.get("moving_time", elapsed)

    # Distance
    dist_str = f"{dist_m / 1000:.2f} km" if dist_m else "—"

    # Duration
    duration_str = _format_duration(moving or elapsed)

    # Pace / speed
    pace_str = ""
    if dist_m and moving:
        if sport in PACE_SPORTS:
            sec_per_km = moving / (dist_m / 1000)
            pace_str = f" | {int(sec_per_km // 60)}:{int(sec_per_km % 60):02d}/km"
        elif sport in SPEED_SPORTS:
            kmh = (dist_m / 1000) / (moving / 3600)
            pace_str = f" | {kmh:.1f} km/h"
        elif sport in SWIM_SPORTS:
            sec_per_100m = moving / (dist_m / 100)
            pace_str = f" | {int(sec_per_100m // 60)}:{int(sec_per_100m % 60):02d}/100m"

    # Heart rate
    hr_str = ""
    avg_hr = act.get("average_heartrate")
    max_hr = act.get("max_heartrate")
    if avg_hr:
        hr_str = f" | HR {int(avg_hr)}"
        if max_hr:
            hr_str += f"/{int(max_hr)}"
        hr_str += " bpm"

    # Elevation
    elev_str = ""
    elev = act.get("total_elevation_gain")
    if elev and elev > 5:
        elev_str = f" | +{int(elev)}m"

    # Suffer score / perceived effort
    suffer = act.get("suffer_score")
    suffer_str = f" | RPE {suffer}" if suffer else ""

    return (
        f"- **{date_str}** [{sport}] *{name}*: "
        f"{dist_str}, {duration_str}{pace_str}{hr_str}{elev_str}{suffer_str}"
    )


def _format_duration(seconds: int) -> str:
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h:
        return f"{h}h {m:02d}m"
    return f"{m}m {s:02d}s"
