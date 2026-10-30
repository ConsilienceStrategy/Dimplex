# Heat pump price limits

The Dimplex Home app colors each hour green, yellow or red by comparing the
German day-ahead spot price with two limits you set by hand. Prices drift week
to week, so fixed limits go stale.

Every afternoon, after tomorrow's prices are out, this tool recomputes both
limits from the last two weeks. It sends a push to your phone only when a
limit should move by 3 ct or more. Most days it stays silent. It never touches
the heat pump.

It runs on GitHub Actions for free, with no device at home.

## Setup (once, about 15 minutes)

1. **Phone.** Install **ntfy** (App Store or Google Play). Tap **+** and
   subscribe to a topic. Treat the topic name like a password: anyone who
   knows it can read your reminders. Make it long and random, e.g.
   `heatpump-k7f2q9xw4m`.
2. **Secret.** On GitHub, open this repository, then **Settings → Secrets and
   variables → Actions → New repository secret**. Name: `NTFY_TOPIC`.
   Value: your topic name.
3. **Permissions.** **Settings → Actions → General**: allow actions, and under
   *Workflow permissions* choose **Read and write permissions**. The tool
   saves its state back to the repository. If the organization locks this
   setting, ask an org admin.
4. **Current limits.** Open `state.json` and check that `green` and `red`
   match what the app shows now.
5. **Test.** **Actions → Heat pump limits → Run workflow**, pick
   **test push**, then **Run workflow**. Your phone should buzz within a
   minute.

That's it. The check runs every day at 13:45 and 16:45 Berlin time in winter,
and 14:45 and 17:45 in summer. The second run only acts if the first found no
prices yet.

## A reminder looks like this

```
Heat pump: set green 8, red 26
Was 10 / 23 ct spot. All-in ≈ 30 / 51 ct.
Last 14 days: 4 charging days, 40% of hours red.
Tomorrow: 6 green · 9 yellow · 9 red.
```

- **Green / red:** the new limits to type into the app, in spot ct/kWh.
- **All-in:** the rough price you pay at those limits (20 ct + 1.19 × spot).
- **Charging days:** days in the window with at least 4 green hours under the
  new limits.
- **Tomorrow:** how tomorrow's 24 hours split up under the new limits.

## If you didn't change the limits

After a reminder, the tool assumes you typed in the new values. If you didn't,
tell it what the app really shows, in one of two ways:

- Tap **Didn't change them?** in the notification (or go to
  **Actions → Heat pump limits → Run workflow**). Pick **set limits**, enter
  green and red, and run it.
- Or edit `state.json` on GitHub (pencil icon), change the two numbers, and
  commit.

## How the limits are computed

The tool looks at the 14 days ending with tomorrow, averaging prices to hours.

- **Green:** for each day it takes the 4th-cheapest hour. Of those 14 values,
  green is set at the 20th percentile. So about one day in five has 4 or
  more hours below green, which is enough to charge the house for the
  next few days.
- **Red:** for each day it takes the 8th-cheapest hour. Red is set at the
  95th percentile of those, so that on most days at least 8 hours stay out
  of red for hot water. The percentile sits high on purpose: prices drift,
  so a limit learned from past days covers fewer of the coming days. In the
  backtest (June to October 2026), 83–89% of days kept 8 hours out of red,
  depending on the period.
- Both are rounded to whole cents, and red stays at least 5 ct above green.

An hour counts as green when its price is below the green limit, and as red
when its price is above the red limit.

## Tuning

All numbers sit in the settings block at the top of `price_limits.py`. Edit
it on GitHub (pencil icon), commit, then run the workflow with **backtest**.
The run's summary page shows which limits the rules would have recommended
over the last 6 weeks, how often you would have been reminded, and how many
charging days and hot-water hours you would have had.

## When something goes wrong

- **No prices by 16:00 Berlin time:** you get one short "no prices today"
  push. It isn't repeated until prices have come back and failed again.
- **The workflow itself fails:** GitHub emails the person who last edited the
  workflow file.
- GitHub sometimes starts scheduled runs late, from a few minutes to an hour.
  That's normal.

## Run it on a computer (optional)

Needs Python 3.9 or newer, with no extra packages.

```
python3 price_limits.py --dry-run     # daily check, prints instead of sending
python3 price_limits.py backtest 6    # replay the last 6 weeks
python3 -m unittest -v                # tests for the limit calculation
```

## Files

- `price_limits.py`: the whole tool, with settings at the top
- `state.json`: the limits set in the app and the last day checked
- `test_price_limits.py`: tests
- `.github/workflows/limits.yml`: the daily schedule and manual actions
- `.github/workflows/tests.yml`: runs the tests when the code changes

Prices come from [Energy-Charts](https://energy-charts.info) (Fraunhofer ISE),
bidding zone DE-LU.
