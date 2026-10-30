#!/usr/bin/env python3
"""Tell me when to retune the Dimplex heat pump's green/red price limits.

Once a day, after tomorrow's day-ahead prices are out, this fetches German
spot prices, recomputes the two limits from recent days, and sends a push via
ntfy when either has drifted from what is set in the app. It never talks to
the heat pump; you change the limits by hand.

  python3 price_limits.py               daily check (the schedule runs this)
  python3 price_limits.py set 9 26      record the limits you actually set
  python3 price_limits.py backtest [W]  replay the last W weeks, send nothing
  python3 price_limits.py test-push     check that notifications arrive

Add --dry-run to print instead of sending and saving.
"""
import json
import math
import os
import sys
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from pathlib import Path
from statistics import mean
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

# --- Settings: tune here -----------------------------------------------------
WINDOW_DAYS = 14         # days the limits learn from, ending with tomorrow
GREEN_HOURS = 5          # a charging day has at least this many green hours
GREEN_PERCENTILE = 22.5  # green limit: this percentile, across days, of each
                         #   day's GREEN_HOURS-th cheapest hour
FREE_HOURS = 8           # hours per day that should stay out of red
RED_PERCENTILE = 95      # red limit: this percentile, across days, of each
                         #   day's FREE_HOURS-th cheapest hour. High on purpose:
                         #   prices drift, so past days overstate future cover
MIN_GAP_CT = 5           # red stays at least this far above green
PING_CT = 3              # remind me when either limit moves this much or more
FEE_BASE_CT = 20         # all-in price ≈ FEE_BASE_CT + FEE_FACTOR × spot
FEE_FACTOR = 1.19
GIVE_UP_HOUR = 16        # Berlin time: no prices by now -> send a failure notice
BACKTEST_WEEKS = 6
# ------------------------------------------------------------------------------

ZONE = "DE-LU"
TZ = ZoneInfo("Europe/Berlin")
PRICE_API = "https://api.energy-charts.info/price"
STATE_FILE = Path(__file__).with_name("state.json")


# --- Prices -------------------------------------------------------------------

def fetch_prices(first: date, last: date) -> list[tuple[int, float]]:
    """(unix seconds, EUR/MWh) covering Berlin days first..last, any step size."""
    url = f"{PRICE_API}?bzn={ZONE}&start={first - timedelta(1)}&end={last + timedelta(1)}"
    request = Request(url, headers={"User-Agent": "dimplex-price-limits"})
    with urlopen(request, timeout=60) as response:
        data = json.load(response)
    unit = data.get("unit", "").replace(" ", "").upper()
    if unit != "EUR/MWH":
        raise ValueError(f"unexpected price unit {data.get('unit')!r}")
    return [(t, p) for t, p in zip(data["unix_seconds"], data["price"]) if p is not None]


def hours_in(day: date) -> int:
    """23, 24 or 25, depending on daylight saving."""
    start, end = (datetime.combine(d, time(), TZ).timestamp() for d in (day, day + timedelta(1)))
    return round((end - start) / 3600)


def daily_hours(prices) -> dict[date, list[float]]:
    """Hourly prices in ct/kWh for each complete Berlin day."""
    by_hour = defaultdict(list)
    for t, eur_mwh in prices:
        by_hour[t - t % 3600].append(eur_mwh / 10)
    days = defaultdict(list)
    for t in sorted(by_hour):
        days[datetime.fromtimestamp(t, TZ).date()].append(mean(by_hour[t]))
    return {d: hours for d, hours in days.items() if len(hours) == hours_in(d)}


# --- Limits -------------------------------------------------------------------

def percentile(values, pct):
    """Linear interpolation between ranks, like PERCENTILE in a spreadsheet."""
    v = sorted(values)
    k = (len(v) - 1) * pct / 100
    lo = math.floor(k)
    hi = min(lo + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (k - lo)


def round_ct(x):
    return math.floor(x + 0.5)


def recommend(days, green_hours=GREEN_HOURS, green_pct=GREEN_PERCENTILE,
              free_hours=FREE_HOURS, red_pct=RED_PERCENTILE, min_gap=MIN_GAP_CT):
    """(green, red) in whole spot ct/kWh from {day: hourly prices}."""
    green = round_ct(percentile([sorted(h)[green_hours - 1] for h in days.values()], green_pct))
    red = round_ct(percentile([sorted(h)[free_hours - 1] for h in days.values()], red_pct))
    return green, max(red, green + min_gap)


def colors(hours, green, red):
    """(green, yellow, red) hour counts: green below the green limit, red above red."""
    g = sum(p < green for p in hours)
    r = sum(p > red for p in hours)
    return g, len(hours) - g - r, r


def needs_change(new, current, ping_ct=PING_CT):
    return any(abs(a - b) >= ping_ct for a, b in zip(new, current))


def all_in(spot):
    return round_ct(FEE_BASE_CT + FEE_FACTOR * spot)


def reminder(new, current, window, tomorrow):
    """(title, body) short enough for a phone lock screen."""
    g, r = new
    counts = [colors(h, g, r) for h in window.values()]
    charging = sum(c[0] >= GREEN_HOURS for c in counts)
    red_share = sum(c[2] for c in counts) / sum(map(len, window.values()))
    tg, ty, tr = colors(tomorrow, g, r)
    title = f"Heat pump: set green {g}, red {r}"
    body = (f"Was {current[0]} / {current[1]} ct spot. All-in ≈ {all_in(g)} / {all_in(r)} ct.\n"
            f"Last {len(window)} days: {charging} charging days, {red_share:.0%} of hours red.\n"
            f"Tomorrow: {tg} green · {ty} yellow · {tr} red.")
    return title, body


# --- Daily check ----------------------------------------------------------------

def check(state, now, fetch=fetch_prices, notify=None):
    """Run the daily check once and return the new state."""
    notify = notify or push
    tomorrow = now.date() + timedelta(1)
    if state.get("checked_for") == str(tomorrow):
        print(f"Already checked prices for {tomorrow}.")
        return state
    try:
        days = daily_hours(fetch(tomorrow - timedelta(WINDOW_DAYS - 1), tomorrow))
        if tomorrow not in days:
            raise LookupError(f"no complete prices for {tomorrow} yet")
        window = {d: h for d, h in days.items() if d > tomorrow - timedelta(WINDOW_DAYS)}
        if len(window) < WINDOW_DAYS // 2:
            raise LookupError(f"only {len(window)} complete days of prices")
    except Exception as err:
        print(f"Price source failed: {err}")
        if now.hour >= GIVE_UP_HOUR and not state.get("failing_since"):
            notify("Heat pump: no prices today",
                   f"Couldn't get tomorrow's spot prices ({err}). "
                   "Limits unchanged; I'll try again tomorrow.")
            state = {**state, "failing_since": str(now.date())}
        return state

    new = recommend(window)
    current = (state["green"], state["red"])
    print(f"{tomorrow}: recommend green {new[0]}, red {new[1]}; set now {current[0]}, {current[1]}")
    state = {**state, "checked_for": str(tomorrow), "failing_since": None}
    if needs_change(new, current):
        notify(*reminder(new, current, window, days[tomorrow]), correction_link=True)
        state.update(green=new[0], red=new[1])
    return state


# --- Backtest -------------------------------------------------------------------

def backtest(state, weeks=BACKTEST_WEEKS, fetch=fetch_prices, today=None):
    """Replay the daily check for each of the last weeks, as if each reminder was followed."""
    last = (today or datetime.now(TZ).date()) + timedelta(1)
    first = last - timedelta(7 * weeks - 1)
    days = daily_hours(fetch(first - timedelta(WINDOW_DAYS), last))
    current = (state["green"], state["red"])
    lines = [f"Starting from green {current[0]}, red {current[1]}.",
             "",
             "prices for  recommend  set in app  green yellow red",
             "            green red  green red   hours"]
    pings, checked, charging, short, red_hours, all_hours = 0, 0, 0, 0, 0, 0
    for i in range(7 * weeks):
        day = first + timedelta(i)
        if day not in days:
            lines.append(f"{day}  no prices")
            continue
        window = {d: h for d, h in days.items() if day - timedelta(WINDOW_DAYS) < d <= day}
        new = recommend(window)
        ping = needs_change(new, current)
        if ping:
            current, pings = new, pings + 1
        g, y, r = colors(days[day], *current)
        checked += 1
        charging += g >= GREEN_HOURS
        short += g + y < FREE_HOURS
        red_hours, all_hours = red_hours + r, all_hours + len(days[day])
        lines.append(f"{day}  {new[0]:>5} {new[1]:>3}  {current[0]:>5} {current[1]:>3}"
                     f"   {g:>5} {y:>6} {r:>3}{'   <- reminder' if ping else ''}")
    lines += ["",
              f"Reminders: {pings} in {weeks} weeks ({pings / weeks:.1f} per week).",
              f"Charging days: {charging} of {checked} ({charging / checked:.0%}); "
              f"aim: 1 in 4-5 (20-25%).",
              f"Days with fewer than {FREE_HOURS} non-red hours: {short} ({short / checked:.0%}); "
              f"aim: 10-15%.",
              f"Hours red: {red_hours / all_hours:.0%}."]
    return "\n".join(lines)


# --- Plumbing -------------------------------------------------------------------

def push(title, body, correction_link=False, dry_run=False):
    if dry_run:
        print(f"[push] {title}\n{body}")
        return
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        sys.exit("NTFY_TOPIC is not set. See README, step 2.")
    headers = {"Title": title, "Tags": "thermometer"}
    repo = os.environ.get("GITHUB_REPOSITORY")
    if correction_link and repo:
        url = f"https://github.com/{repo}/actions/workflows/limits.yml"
        headers["Actions"] = f"view, Didn't change them?, {url}"
    request = Request(f"https://ntfy.sh/{topic}", data=body.encode(), headers=headers)
    urlopen(request, timeout=30).close()


def load_state():
    return json.loads(STATE_FILE.read_text())


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2) + "\n")


def main(argv):
    dry_run = "--dry-run" in argv
    args = [a for a in argv if a != "--dry-run"]
    command = args[0] if args else "check"
    state = load_state()

    if command == "check":
        notify = lambda title, body, **kw: push(title, body, dry_run=dry_run, **kw)
        new_state = check(state, datetime.now(TZ), notify=notify)
    elif command == "set":
        try:
            green, red = int(args[1]), int(args[2])
        except (IndexError, ValueError):
            sys.exit("Give both limits in whole cents, e.g.: set 9 26")
        new_state = {**state, "green": green, "red": red}
        print(f"Recorded green {green}, red {red}.")
    elif command == "backtest":
        report = backtest(state, int(args[1]) if len(args) > 1 else BACKTEST_WEEKS)
        print(report)
        if os.environ.get("GITHUB_STEP_SUMMARY"):
            with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
                f.write(f"```\n{report}\n```\n")
        return
    elif command == "test-push":
        push("Heat pump reminders work",
             "Test from price_limits.py. Limit reminders will arrive here.", dry_run=dry_run)
        return
    else:
        sys.exit(__doc__)

    if new_state != state and not dry_run:
        save_state(new_state)


if __name__ == "__main__":
    main(sys.argv[1:])
