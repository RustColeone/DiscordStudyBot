"""python3 -m clubnotes list|next <schedule file>"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

from .schedule import next_meeting, parse, sort_week


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="clubnotes", description="A club's weekly meetings")
    parser.add_argument("command", choices=["list", "next"])
    parser.add_argument("file", type=Path)
    arguments = parser.parse_args(argv)
    meetings = parse(arguments.file.read_text(encoding="utf-8"))
    if arguments.command == "list":
        for meeting in sort_week(meetings):
            print(f"{meeting.day} {meeting.time}  {meeting.name}")
    else:
        print(f"Next: {next_meeting(meetings, dt.datetime.now())}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
