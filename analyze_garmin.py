#!/usr/bin/env python3
"""
Garmin data analyzer for Hyrox training plan adaptation.

Usage:
    python3 analyze_garmin.py [--csv activities.csv] [--week N]

Drop a fresh CSV export from Garmin Connect each week and re-run.
The script updates training targets in training-plan-current.md.
"""

import csv
import json
import os
import sys
import argparse
from datetime import datetime, timedelta
from pathlib import Path

RACE_DATE = datetime(2026, 7, 5)
PLAN_START = datetime(2026, 5, 19)
CSV_PATH = Path(__file__).parent / "garmin-activities.csv"
OUTPUT_PATH = Path(__file__).parent / "weekly-targets.md"

# ── Heart rate zone calculation ──────────────────────────────────────────────

def estimate_max_hr(runs):
    """
    Best estimate of true max HR from activity data.
    Ignores obvious spike outliers (>210) and uses 98th percentile.
    """
    max_hrs = sorted(
        [r["max_hr"] for r in runs if r["max_hr"] and r["max_hr"] < 200],
        reverse=True,
    )
    if not max_hrs:
        return 185  # fallback
    # use average of top 3 values as a conservative true max
    top = max_hrs[:3]
    return round(sum(top) / len(top))


def hr_zones(max_hr, resting_hr=55):
    """
    5-zone model using heart rate reserve (Karvonen method).
    """
    hrr = max_hr - resting_hr
    zones = {
        "Z1 Recovery":       (resting_hr + round(hrr * 0.50), resting_hr + round(hrr * 0.60)),
        "Z2 Aerobic base":   (resting_hr + round(hrr * 0.60), resting_hr + round(hrr * 0.70)),
        "Z3 Aerobic thresh": (resting_hr + round(hrr * 0.70), resting_hr + round(hrr * 0.80)),
        "Z4 Threshold":      (resting_hr + round(hrr * 0.80), resting_hr + round(hrr * 0.90)),
        "Z5 VO2max":         (resting_hr + round(hrr * 0.90), max_hr),
    }
    return zones


# ── CSV parsing ──────────────────────────────────────────────────────────────

def pace_to_seconds(pace_str):
    """Convert 'mm:ss' pace string to seconds per km."""
    if not pace_str or pace_str in ("--", "--:--:--"):
        return None
    parts = pace_str.strip().split(":")
    if len(parts) == 2:
        return int(parts[0]) * 60 + int(parts[1])
    return None


def seconds_to_pace(secs):
    """Convert seconds per km back to mm:ss string."""
    if secs is None:
        return "--:--"
    return f"{secs // 60}:{secs % 60:02d}"


def load_activities(csv_path):
    runs = []
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if not row.get("Date"):
                continue
            try:
                date = datetime.strptime(row["Date"].strip(), "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue

            activity_type = row.get("Activity Type", "").strip()
            if "Run" not in activity_type:
                continue

            try:
                dist = float(row["Distance"].replace(",", ""))
            except (ValueError, AttributeError):
                dist = 0.0

            def safe_int(val):
                v = val.strip() if val else ""
                return int(v) if v and v != "--" else None

            runs.append({
                "date": date,
                "type": activity_type,
                "title": row.get("Title", "").strip(),
                "distance": dist,
                "avg_hr": safe_int(row.get("Avg HR", "")),
                "max_hr": safe_int(row.get("Max HR", "")),
                "avg_pace_sec": pace_to_seconds(row.get("Avg Pace", "")),
                "calories": row.get("Calories", ""),
            })

    runs.sort(key=lambda x: x["date"])
    return runs


# ── Analysis functions ────────────────────────────────────────────────────────

def filter_recent(runs, from_year=2020):
    return [r for r in runs if r["date"].year >= from_year]


def current_week_number():
    today = datetime.now()
    delta = today - PLAN_START
    week = delta.days // 7 + 1
    return max(1, min(6, week))


def sessions_this_week(runs, week_num):
    week_start = PLAN_START + timedelta(weeks=week_num - 1)
    week_end = week_start + timedelta(days=7)
    return [r for r in runs if week_start <= r["date"] < week_end]


def aerobic_pace_from_hr(runs, target_hr_range):
    """
    Estimate current easy-pace from runs where avg HR is in the target range.
    Uses only runs >= 3km to filter out warm-up noise.
    """
    lo, hi = target_hr_range
    eligible = [
        r for r in runs
        if r["avg_hr"] and lo <= r["avg_hr"] <= hi
        and r["distance"] >= 3.0
        and r["avg_pace_sec"]
    ]
    if not eligible:
        return None
    paces = [r["avg_pace_sec"] for r in eligible]
    return round(sum(paces) / len(paces))


def estimated_5k_pace(runs):
    """
    Best-effort 5k pace estimate from the available data.
    Looks at recent 5-10km efforts and extrapolates.
    """
    candidates = [
        r for r in runs
        if 4.5 <= r["distance"] <= 12.0
        and r["avg_pace_sec"]
        and r["avg_hr"]
        and r["avg_hr"] > 140  # must be an honest effort
    ]
    if not candidates:
        return None
    # use the fastest recent effort
    candidates.sort(key=lambda x: x["avg_pace_sec"])
    return candidates[0]["avg_pace_sec"]


def training_load_summary(runs, n_weeks=4):
    cutoff = datetime.now() - timedelta(weeks=n_weeks)
    recent = [r for r in runs if r["date"] >= cutoff]
    total_km = sum(r["distance"] for r in recent)
    sessions = len(recent)
    weekly_avg = total_km / n_weeks
    return {"total_km": total_km, "sessions": sessions, "weekly_avg_km": weekly_avg}


def fitness_trend(runs):
    """
    Compare pace at similar HR across last 2 weeks vs previous 2 weeks.
    Positive trend = getting faster at same HR (aerobic adaptation).
    """
    now = datetime.now()
    recent_2w = [r for r in runs if r["date"] >= now - timedelta(weeks=2) and r["avg_hr"] and r["avg_pace_sec"]]
    prev_2w = [
        r for r in runs
        if now - timedelta(weeks=4) <= r["date"] < now - timedelta(weeks=2)
        and r["avg_hr"] and r["avg_pace_sec"]
    ]
    if len(recent_2w) < 2 or len(prev_2w) < 2:
        return None

    def avg_pace_at_hr(sessions, hr_lo=140, hr_hi=160):
        matching = [s for s in sessions if hr_lo <= s["avg_hr"] <= hr_hi and s["avg_pace_sec"]]
        if not matching:
            return None
        return sum(s["avg_pace_sec"] for s in matching) / len(matching)

    recent_pace = avg_pace_at_hr(recent_2w)
    prev_pace = avg_pace_at_hr(prev_2w)
    if recent_pace is None or prev_pace is None:
        return None
    diff = prev_pace - recent_pace  # positive = faster
    return diff


# ── Adaptive pace targets ─────────────────────────────────────────────────────

def adaptive_targets(runs, week_num, max_hr, zones):
    """
    Generate this week's pace targets based on actual training data.
    Falls back to conservative defaults if data is sparse.
    """
    z2_lo, z2_hi = zones["Z2 Aerobic base"]
    z4_lo, z4_hi = zones["Z4 Threshold"]

    z2_pace = aerobic_pace_from_hr(runs, (z2_lo, z2_hi))
    effort_pace = estimated_5k_pace(runs)

    # Conservative defaults if no recent data
    if not z2_pace:
        z2_pace = 420  # 7:00/km — safe default for detraining + post-surgery
    if not effort_pace:
        effort_pace = 344  # 5:44/km — last known race-ish pace

    # Gradual pace progression target: close 12s/km gap to 5:30 over 5 weeks
    race_target = 330  # 5:30/km in seconds
    current_gap = effort_pace - race_target
    week_increment = current_gap / 5
    weekly_interval_target = round(effort_pace - (week_increment * (week_num - 1)))
    weekly_interval_target = max(weekly_interval_target, race_target)

    # Easy runs: always based on Z2, with a cap
    easy_pace = max(z2_pace, 360)  # never faster than 6:00/km for easy

    # Tempo: midpoint between easy and interval
    tempo_pace = round((easy_pace + weekly_interval_target) / 2)

    return {
        "week": week_num,
        "easy": easy_pace,
        "tempo": tempo_pace,
        "intervals": weekly_interval_target,
        "race_target": race_target,
        "z2_range": (z2_lo, z2_hi),
        "z4_range": (z4_lo, z4_hi),
    }


# ── Weekly volume prescription ────────────────────────────────────────────────

VOLUME_PLAN = {
    # week: (total_km, long_run_km, interval_reps)
    1: (15, 4,  4),
    2: (20, 6,  6),
    3: (25, 7,  6),
    4: (28, 8,  8),
    5: (22, 6,  5),
    6: (10, 4,  3),
}


# ── Output generator ──────────────────────────────────────────────────────────

def generate_weekly_targets(runs, week_num, max_hr, zones, targets, load):
    vol = VOLUME_PLAN.get(week_num, VOLUME_PLAN[6])
    total_km, long_km, reps = vol

    trend = fitness_trend(runs)
    trend_str = "Not enough recent data to calculate trend."
    if trend is not None:
        if trend > 5:
            trend_str = f"IMPROVING — you're running {abs(trend):.0f}s/km faster at the same HR vs last 2 weeks. Progression is on track."
        elif trend < -5:
            trend_str = f"SLOWER — pace at same HR has dropped {abs(trend):.0f}s/km vs last 2 weeks. Likely fatigue or under-recovery. Consider an extra rest day."
        else:
            trend_str = "STABLE — pace at same HR is unchanged. Aerobic adaptation is accumulating, results typically show after 3–4 weeks."

    z2_lo, z2_hi = targets["z2_range"]

    lines = [
        f"# Week {week_num} Training Targets",
        f"*Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')} | Race in {(RACE_DATE - datetime.now()).days} days*",
        "",
        "## Fitness trend",
        trend_str,
        "",
        "## This week's pace targets",
        "",
        "| Session type | Target pace | HR guide |",
        "|---|---|---|",
        f"| Easy / Z2 runs | {seconds_to_pace(targets['easy'])}/km | {z2_lo}–{z2_hi} bpm |",
        f"| Tempo | {seconds_to_pace(targets['tempo'])}/km | {targets['z4_range'][0]}–{targets['z4_range'][1]} bpm |",
        f"| 1km intervals | {seconds_to_pace(targets['intervals'])}/km | Max effort for duration |",
        f"| Race pace target | {seconds_to_pace(targets['race_target'])}/km | Hold across all 8 runs |",
        "",
        "## Volume prescription",
        "",
        f"| Metric | Target |",
        f"|---|---|",
        f"| Total run volume | {total_km} km |",
        f"| Longest single run | {long_km} km |",
        f"| 1km interval reps | {reps} |",
        "",
        "## Recent training load (last 4 weeks)",
        "",
        f"| Metric | Value |",
        f"|---|---|",
        f"| Total km logged | {load['total_km']:.1f} km |",
        f"| Sessions | {load['sessions']} |",
        f"| Weekly average | {load['weekly_avg_km']:.1f} km |",
        "",
        "## This week's sessions",
        "",
        "### Monday — Run A (Intervals)",
        f"- Warm-up: 10 min easy @ {seconds_to_pace(targets['easy'])}/km",
        f"- Main: {reps} × 1km @ **{seconds_to_pace(targets['intervals'])}/km** | rest 90s (W1–2) or 60s (W3+)",
        f"- Cool-down: 1km easy jog",
        "",
        "### Wednesday — Strength + Stations",
        "- See training-plan.md for this week's strength block and station prescription",
        f"- SkiErg HR guide: keep below {targets['z4_range'][1]} bpm on station reps",
        "",
        "### Friday — Run B (Easy aerobic)",
        f"- {round(total_km * 0.30):.0f}km continuous @ {seconds_to_pace(targets['easy'])}/km",
        f"- HR should stay between {z2_lo}–{z2_hi} bpm the entire run",
        f"- Finish with 4 × 100m strides",
        "",
        "### Saturday — Simulation",
        f"- Refer to training-plan.md Week {week_num} Saturday session",
        f"- Run pace target during simulation: {seconds_to_pace(targets['intervals'])}/km",
        "",
        "---",
        "",
        "## How to update this file",
        "1. Export your Garmin Connect activities as CSV (Activities → Export to CSV)",
        "2. Replace `garmin-activities.csv` in this folder with the new file",
        "3. Run: `python3 analyze_garmin.py`",
        "4. This file will be regenerated with updated targets based on your actual runs.",
    ]

    return "\n".join(lines)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Analyze Garmin data and generate weekly training targets")
    parser.add_argument("--csv", default=str(CSV_PATH), help="Path to Garmin CSV export")
    parser.add_argument("--week", type=int, default=None, help="Override week number (1–6)")
    parser.add_argument("--resting-hr", type=int, default=55, help="Your resting heart rate")
    args = parser.parse_args()

    csv_file = Path(args.csv)
    if not csv_file.exists():
        print(f"ERROR: CSV not found at {csv_file}")
        sys.exit(1)

    all_runs = load_activities(csv_file)
    recent_runs = filter_recent(all_runs, from_year=2020)

    max_hr = estimate_max_hr(all_runs)
    zones = hr_zones(max_hr, resting_hr=args.resting_hr)
    week_num = args.week or current_week_number()
    targets = adaptive_targets(recent_runs, week_num, max_hr, zones)
    load = training_load_summary(recent_runs)

    # ── Print summary to terminal ──
    print("\n" + "=" * 60)
    print(f"  HYROX TRAINING — WEEK {week_num} ANALYSIS")
    print(f"  Race date: {RACE_DATE.strftime('%d %b %Y')}  ({(RACE_DATE - datetime.now()).days} days away)")
    print("=" * 60)

    print(f"\n  Max HR (estimated):    {max_hr} bpm")
    print(f"  Resting HR (input):    {args.resting_hr} bpm")
    print(f"\n  Heart Rate Zones:")
    for name, (lo, hi) in zones.items():
        print(f"    {name:<22} {lo}–{hi} bpm")

    print(f"\n  Total recorded runs (2020–now): {len(recent_runs)}")
    print(f"  Most recent run:  {recent_runs[-1]['date'].strftime('%Y-%m-%d') if recent_runs else 'None'}")
    if recent_runs:
        last = recent_runs[-1]
        print(f"    {last['distance']:.1f}km @ {seconds_to_pace(last['avg_pace_sec'])}/km, HR {last['avg_hr']}")

    print(f"\n  Week {week_num} pace targets:")
    print(f"    Easy:       {seconds_to_pace(targets['easy'])}/km  (Z2: {targets['z2_range'][0]}–{targets['z2_range'][1]} bpm)")
    print(f"    Tempo:      {seconds_to_pace(targets['tempo'])}/km")
    print(f"    Intervals:  {seconds_to_pace(targets['intervals'])}/km")
    print(f"    Race goal:  {seconds_to_pace(targets['race_target'])}/km")

    print(f"\n  Generating {OUTPUT_PATH.name}...")

    output = generate_weekly_targets(recent_runs, week_num, max_hr, zones, targets, load)
    OUTPUT_PATH.write_text(output)
    print(f"  Done → {OUTPUT_PATH}")
    print()


if __name__ == "__main__":
    main()
