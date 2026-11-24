#!/usr/bin/env python3
"""Scheduling self-check. Run: python3 test_collector.py"""

import importlib
import os
import sys
import tempfile

os.environ["DATA_DIR"] = tempfile.mkdtemp()
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def load(interval):
    os.environ.pop("INTERVAL_HOURS", None)
    if interval is not None:
        os.environ["INTERVAL_HOURS"] = interval
    import collector
    return importlib.reload(collector)


def test_interval():
    delay, target = load("6").next_run()
    assert delay == 6 * 3600, delay


def test_default_is_nightly_window():
    for interval in (None, "", "0"):
        delay, target = load(interval).next_run()
        assert 0 < delay <= 27 * 3600, delay
        assert 2 <= target.hour < 5, target


if __name__ == "__main__":
    test_interval()
    test_default_is_nightly_window()
    print("ok")
