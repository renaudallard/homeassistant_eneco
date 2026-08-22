#!/usr/bin/env python3
# Copyright (c) 2026 Renaud Allard <renaud@allard.it>
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# 1. Redistributions of source code must retain the above copyright notice,
#    this list of conditions and the following disclaimer.
#
# 2. Redistributions in binary form must reproduce the above copyright notice,
#    this list of conditions and the following disclaimer in the documentation
#    and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.

"""Check that every translation matches strings.json and the code that uses it."""

import json
import pathlib
import re
import sys

BASE = pathlib.Path(__file__).resolve().parent.parent / "custom_components" / "eneco_be"


def keys(node: object, prefix: str = "") -> set[str]:
    """Return every dotted key path in a nested mapping."""
    if not isinstance(node, dict):
        return set()
    found: set[str] = set()
    for key, value in node.items():
        found.add(f"{prefix}.{key}")
        found |= keys(value, f"{prefix}.{key}")
    return found


def main() -> int:
    """Report every mismatch and return a shell exit status."""
    strings = json.loads((BASE / "strings.json").read_text(encoding="utf-8"))
    expected = keys(strings)
    problems: list[str] = []

    for path in sorted((BASE / "translations").glob("*.json")):
        found = keys(json.loads(path.read_text(encoding="utf-8")))
        problems += [f"{path.name}: missing {k}" for k in sorted(expected - found)]
        problems += [f"{path.name}: unknown {k}" for k in sorted(found - expected)]

    flow = (BASE / "config_flow.py").read_text(encoding="utf-8")
    declared_steps = set(strings["config"]["step"]) | set(strings["options"]["step"])
    problems += [
        f"config_flow.py: step {s} has no strings entry"
        for s in sorted(set(re.findall(r'step_id="([a-z_]+)"', flow)) - declared_steps)
    ]

    aborts = set(re.findall(r'reason="([a-z_]+)"', flow)) | {
        "already_configured",
        "reauth_successful",
        "reconfigure_successful",
        "unique_id_mismatch",
    }
    problems += [
        f"config_flow.py: abort reason {r} has no strings entry"
        for r in sorted(aborts - set(strings["config"]["abort"]))
    ]

    errors = set(re.findall(r'errors\["base"\] = "([a-z_]+)"', flow))
    problems += [
        f"config_flow.py: error {e} has no strings entry"
        for e in sorted(errors - set(strings["config"]["error"]))
    ]

    # Every platform that declares entity names, so adding one to strings.json
    # brings its module along rather than quietly going unchecked.
    entities = ""
    declared: set[str] = set()
    for platform in strings["entity"]:
        entities += (BASE / f"{platform}.py").read_text(encoding="utf-8")
        declared |= set(strings["entity"][platform])
    problems += [
        f"entity translation_key {t} has no strings entry"
        for t in sorted(
            set(re.findall(r'translation_key="([a-z_]+)"', entities)) - declared
        )
    ]

    # An enum sensor renders through its state translations, so a status the
    # code can report and strings.json cannot name shows up raw.
    states = set(strings["entity"]["sensor"].get("status", {}).get("state", {}))
    reported = set(
        re.findall(
            r'^STATUS_[A-Z_]+ = "([a-z_]+)"$',
            (BASE / "sensor.py").read_text(encoding="utf-8"),
            re.MULTILINE,
        )
    )
    problems += [
        f"sensor.py: status {v} has no state translation"
        for v in sorted(reported - states)
    ]
    problems += [
        f"strings.json: status state {v} is not reported by sensor.py"
        for v in sorted(states - reported)
    ]

    icons = json.loads((BASE / "icons.json").read_text(encoding="utf-8"))
    for platform, entries in icons["entity"].items():
        declared = set(strings["entity"].get(platform, {}))
        problems += [
            f"icons.json: {platform}.{k} has no strings entry"
            for k in sorted(set(entries) - declared)
        ]
        problems += [
            f"icons.json: {platform}.{k} has no icon"
            for k in sorted(declared - set(entries))
        ]

    for problem in problems:
        print(problem)
    print(f"{len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
