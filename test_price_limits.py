"""Tests for the limit calculation. Run: python3 -m unittest -v"""
import unittest
from datetime import date, datetime, time, timedelta

import price_limits as pl


def as_prices(days):
    """{day: hourly ct/kWh} -> [(unix seconds, EUR/MWh)] in 15-minute steps."""
    out = []
    for day, hours in days.items():
        start = int(datetime.combine(day, time(), pl.TZ).timestamp())
        for h, ct in enumerate(hours):
            out += [(start + 3600 * h + 900 * q, ct * 10) for q in range(4)]
    return out


class Hours(unittest.TestCase):
    def test_quarters_average_to_hours_in_ct(self):
        start = int(datetime(2026, 10, 10, tzinfo=pl.TZ).timestamp())
        prices = [(start + 900 * i, 100.0 + 10 * (i % 4)) for i in range(96)]
        self.assertEqual(pl.daily_hours(prices), {date(2026, 10, 10): [11.5] * 24})

    def test_daylight_saving_days(self):
        for day, n in [(date(2026, 3, 29), 23), (date(2026, 10, 25), 25), (date(2026, 10, 26), 24)]:
            self.assertEqual(pl.hours_in(day), n)
            days = pl.daily_hours(as_prices({day: [5.0] * n}))
            self.assertEqual(len(days[day]), n)

    def test_incomplete_day_is_dropped(self):
        self.assertEqual(pl.daily_hours(as_prices({date(2026, 10, 10): [5.0] * 23})), {})


class Limits(unittest.TestCase):
    def test_percentile_interpolates(self):
        self.assertEqual(pl.percentile([1, 2, 3, 4, 5], 50), 3)
        self.assertEqual(pl.percentile([10, 0], 25), 2.5)
        self.assertEqual(pl.percentile([7], 90), 7)

    def test_rounds_half_up(self):
        self.assertEqual([pl.round_ct(x) for x in (2.5, 3.49, -0.5, -0.51)], [3, 3, 0, -1])

    def test_uses_nth_cheapest_hour_and_percentiles(self):
        # Day i costs i ct in its cheap hours and 50 ct otherwise.
        days = {date(2026, 9, 1) + timedelta(i): [i] * 6 + [50.0] * 18 for i in range(20)}
        green, red = pl.recommend(days, green_hours=5, green_pct=25, free_hours=4, red_pct=90)
        self.assertEqual((green, red), (5, 17))  # 25th pct of 0..19 = 4.75; 90th = 17.1

    def test_ranks_within_a_day(self):
        days = {date(2026, 9, 1): [float(h) for h in reversed(range(24))]}
        green, red = pl.recommend(days, green_hours=5, free_hours=12, min_gap=5)
        self.assertEqual((green, red), (4, 11))

    def test_red_stays_above_green(self):
        days = {date(2026, 9, 1): [float(h) for h in range(24)]}
        self.assertEqual(pl.recommend(days, green_hours=5, free_hours=4, min_gap=5), (4, 9))

    def test_colors(self):
        self.assertEqual(pl.colors([5, 9.9, 10, 25, 25.1, 30], green=10, red=25), (2, 2, 2))

    def test_ping_threshold(self):
        self.assertTrue(pl.needs_change((12, 23), (10, 23), ping_ct=2))
        self.assertTrue(pl.needs_change((10, 21), (10, 23), ping_ct=2))
        self.assertFalse(pl.needs_change((11, 24), (10, 23), ping_ct=2))

    def test_all_in(self):
        self.assertEqual(pl.all_in(10), 32)  # 20 + 11.9


class DailyCheck(unittest.TestCase):
    now = datetime(2026, 10, 30, 14, 0, tzinfo=pl.TZ)
    tomorrow = date(2026, 10, 31)

    def prices(self, cheap=8.0, last_day=None):
        last_day = last_day or self.tomorrow
        days = [last_day - timedelta(i) for i in range(30)]
        days = {d: [cheap] * 10 + [40.0] * (pl.hours_in(d) - 10) for d in days}
        return lambda first, last: as_prices(days)

    def run_check(self, state, fetch, now=None):
        sent = []
        new = pl.check(state, now or self.now, fetch=fetch,
                       notify=lambda title, body, **kw: sent.append((title, body)))
        return new, sent

    def test_reminds_and_assumes_new_limits_applied(self):
        state = {"green": 20, "red": 40, "checked_for": None, "failing_since": None}
        new, sent = self.run_check(state, self.prices(cheap=8.0))
        self.assertEqual((new["green"], new["red"]), (8, 13))
        self.assertEqual(new["checked_for"], "2026-10-31")
        self.assertEqual(len(sent), 1)
        title, body = sent[0]
        self.assertEqual(title, "Heat pump: set green 8, red 13")
        self.assertIn("Was 20 / 40 ct", body)
        self.assertIn("Tomorrow: 0 green · 10 yellow · 14 red.", body)

    def test_silent_when_close(self):
        state = {"green": 9, "red": 14, "checked_for": None, "failing_since": None}
        new, sent = self.run_check(state, self.prices(cheap=8.0))
        self.assertEqual(sent, [])
        self.assertEqual((new["green"], new["red"]), (9, 14))

    def test_runs_once_per_day(self):
        state = {"green": 20, "red": 40, "checked_for": "2026-10-31", "failing_since": None}
        new, sent = self.run_check(state, lambda *a: self.fail("should not fetch"))
        self.assertEqual((new, sent), (state, []))

    def test_waits_quietly_for_late_prices(self):
        state = {"green": 10, "red": 23, "checked_for": None, "failing_since": None}
        new, sent = self.run_check(state, self.prices(last_day=self.now.date()))
        self.assertEqual((new, sent), (state, []))

    def test_one_failure_notice_per_outage(self):
        def broken(*a):
            raise OSError("connection refused")
        state = {"green": 10, "red": 23, "checked_for": None, "failing_since": None}
        evening = self.now.replace(hour=17)
        state, sent = self.run_check(state, broken, now=evening)
        self.assertEqual(len(sent), 1)
        self.assertIn("connection refused", sent[0][1])
        state, sent = self.run_check(state, broken, now=evening + timedelta(days=1))
        self.assertEqual(sent, [])
        state, sent = self.run_check(state, self.prices(last_day=date(2026, 11, 1)),
                                     now=evening + timedelta(days=1))
        self.assertIsNone(state["failing_since"])


if __name__ == "__main__":
    unittest.main()
