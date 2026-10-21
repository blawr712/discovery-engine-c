from datetime import datetime
from pathlib import Path
import unittest
import xml.etree.ElementTree as ElementTree

from src.monthly_schedule import RUN_DAYS, build_task_xml, first_start

NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}


class FirstStartTests(unittest.TestCase):
    def test_registering_inside_the_window_starts_next_month(self):
        """This month's cycle may already be done by hand or under way."""
        self.assertEqual(first_start(datetime(2026, 10, 21, 13)), datetime(2026, 11, 1))
        self.assertEqual(first_start(datetime(2026, 10, 16)), datetime(2026, 11, 1))
        self.assertEqual(first_start(datetime(2026, 12, 30)), datetime(2027, 1, 1))

    def test_registering_before_the_window_includes_this_month(self):
        self.assertEqual(first_start(datetime(2026, 11, 3, 9)), datetime(2026, 11, 3))


class TaskXmlTests(unittest.TestCase):
    def setUp(self):
        text = build_task_xml(
            "PC\\brad", Path(r"C:\repo\scripts\run_monthly.cmd"), Path(r"C:\repo"),
            datetime(2026, 11, 1),
        )
        self.assertTrue(text.startswith('<?xml version="1.0" encoding="UTF-16"?>'))
        # ElementTree refuses an encoding declaration on a str; parse the body.
        self.root = ElementTree.fromstring(text.split("?>", 1)[1])

    def _text(self, path):
        return self.root.find(path, NS).text

    def test_fires_on_each_retry_day_of_every_month_from_the_start(self):
        days = [int(day.text) for day in self.root.findall(".//t:DaysOfMonth/t:Day", NS)]
        self.assertEqual(days, list(RUN_DAYS))
        self.assertEqual(len(self.root.find(".//t:Months", NS)), 12)
        self.assertEqual(self._text(".//t:StartBoundary"), "2026-11-01T02:00:00")

    def test_runs_as_the_user_without_a_password_or_elevation(self):
        self.assertEqual(self._text(".//t:Principal/t:UserId"), "PC\\brad")
        self.assertEqual(self._text(".//t:Principal/t:LogonType"), "InteractiveToken")
        self.assertEqual(self._text(".//t:Principal/t:RunLevel"), "LeastPrivilege")

    def test_settings_suit_a_long_resumable_job(self):
        self.assertEqual(self._text(".//t:MultipleInstancesPolicy"), "IgnoreNew")
        self.assertEqual(self._text(".//t:StartWhenAvailable"), "true")
        self.assertEqual(self._text(".//t:RunOnlyIfNetworkAvailable"), "true")
        self.assertEqual(self._text(".//t:ExecutionTimeLimit"), "PT36H")

    def test_runs_the_logging_wrapper_from_the_repository(self):
        self.assertEqual(self._text(".//t:Exec/t:Command"), r"C:\repo\scripts\run_monthly.cmd")
        self.assertEqual(self._text(".//t:Exec/t:WorkingDirectory"), r"C:\repo")

    def test_special_characters_are_escaped(self):
        text = build_task_xml("A&B\\<me>", Path(r"C:\r&d\run.cmd"), Path(r"C:\r&d"), datetime(2026, 11, 1))
        root = ElementTree.fromstring(text.split("?>", 1)[1])
        self.assertEqual(root.find(".//t:UserId", NS).text, "A&B\\<me>")


if __name__ == "__main__":
    unittest.main()
