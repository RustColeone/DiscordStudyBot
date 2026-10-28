import datetime as dt
import unittest

from clubnotes.schedule import Meeting, next_meeting, parse, sort_week

TEXT = """# day time name
Mon 15:30 Chess
Wed 16:00 Robotics
Fri 12:00 Choir
"""


class ParseTests(unittest.TestCase):
    def test_reads_meetings_and_skips_comments(self):
        self.assertEqual(parse(TEXT), [Meeting("Mon", "15:30", "Chess"), Meeting("Wed", "16:00", "Robotics"),
                                       Meeting("Fri", "12:00", "Choir")])

    def test_names_may_have_spaces(self):
        self.assertEqual(parse("Tue 09:00 Book club")[0].name, "Book club")

    def test_rejects_unknown_days(self):
        with self.assertRaises(ValueError):
            parse("Someday 10:00 Nap")


class WeekTests(unittest.TestCase):
    def test_sorts_by_day_then_time(self):
        meetings = parse("Wed 16:00 Robotics\nMon 15:30 Chess\nMon 09:00 Breakfast")
        self.assertEqual([m.name for m in sort_week(meetings)], ["Breakfast", "Chess", "Robotics"])

    def test_next_meeting_later_this_week(self):
        tuesday = dt.datetime(2026, 10, 20, 12, 0)
        self.assertEqual(next_meeting(parse(TEXT), tuesday).name, "Robotics")

    def test_next_meeting_wraps_to_next_week(self):
        saturday = dt.datetime(2026, 10, 24, 12, 0)
        self.assertEqual(next_meeting(parse(TEXT), saturday).name, "Chess")


if __name__ == "__main__":
    unittest.main()
