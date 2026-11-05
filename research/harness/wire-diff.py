"""Compare the ns2jb-built frames (wire-selftest.cjs) with the Python port (wire-selftest.py)."""
import json
import sys
from pathlib import Path

here = Path(__file__).with_name
js = json.loads(here("selftest-js.json").read_text())
py = json.loads(here("selftest-py.json").read_text())

issues = []
js_names = {case["name"]: case for case in js["cases"]}
py_names = {case["name"]: case for case in py["cases"]}
if js_names.keys() != py_names.keys():
    issues.append("case name sets differ")
for name, expected in js_names.items():
    actual = py_names[name]
    if expected["sha"] != actual["sha"]:
        issues.append("frame differs: %s\n  ns2jb %s\n  python %s" % (name, expected["hex"], actual["hex"]))

js_replies = {reply["name"]: reply for reply in js["replies"]}
py_replies = {reply["name"]: reply for reply in py["replies"]}
if js_replies.keys() != py_replies.keys():
    issues.append("reply name sets differ")
for name, expected in js_replies.items():
    actual = py_replies[name]
    if expected["sha"] != actual["sha"]:
        issues.append("reply bytes differ: " + name)
    if expected["parsed"] != actual["parsed"]:
        issues.append("reply parse differs: %s\n  ns2jb %s\n  python %s" % (name, expected["parsed"], actual["parsed"]))

print(json.dumps({"frames": len(js_names), "replies": len(js_replies),
                  "identical": not issues, "issues": issues}, indent=2))
raise SystemExit(1 if issues else 0)
