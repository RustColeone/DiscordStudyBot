# clubnotes

A tiny tool for a school club's weekly meetings: a schedule file with one meeting per line, and a command line
to list them or find the next one. It is Chimera's practice project: small, with tests, and with a few
things left to do.

```
$ python3 -m clubnotes list schedule.txt
Mon 15:30  Chess
Wed 16:00  Robotics
Sun 10:00  Hiking

$ python3 -m clubnotes next schedule.txt
Next: Wed 16:00 Robotics
```

Tests: `python3 -m unittest discover -s tests -t .`

## Things to try with the club

* "Meetings on Sunday are listed first instead of last. Fix it and add a test."
* "Add a `--json` flag to `list` that prints the meetings as JSON."
* "What does `next_meeting` do when the schedule is empty?" (a question: nothing changes)
* "Let a meeting line carry a room, like `Mon 15:30 Chess @ Room 204`, and show it in `list`."
