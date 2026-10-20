#!/usr/bin/env python3
"""Find dangling branches and broken references in an Amazon Connect flow.

The Connect designer will happily save a flow with an unconnected Success or
Error branch. At runtime a contact reaching that branch has nowhere to go and
the call ends -- which the caller experiences as a silent disconnect, and
which looks random because it depends on which path they took.

This walks the exported JSON and fails the build on:

  DANGLING   a block with an outbound branch that goes nowhere
  BROKEN     a branch pointing at an identifier that does not exist

Usage:
    python3 scripts/audit_flow.py terraform/flows/main.json
    python3 scripts/audit_flow.py flows/*.json

Exit code 1 on any finding, so it works as a pre-deploy gate in CI.
"""

from __future__ import annotations

import json
import pathlib
import sys

# Blocks that legitimately end a path. Anything else with no outbound
# transition is a dead end.
TERMINAL = {
    "DisconnectParticipant",
    "EndFlowExecution",
    "EndFlowModuleExecution",  # 'Return' / Exit module
    "TransferContactToQueue",
    "TransferToFlow",
}


def audit(path: pathlib.Path) -> int:
    flow = json.loads(path.read_text())
    actions = flow.get("Actions", [])
    ids = {a["Identifier"] for a in actions}

    dangling: list[tuple[str, str, str]] = []
    broken: list[tuple[str, str, str]] = []

    for action in actions:
        aid = action["Identifier"]
        atype = action["Type"]
        transitions = action.get("Transitions") or {}

        targets: list[tuple[str, str | None]] = []

        if transitions.get("NextAction"):
            targets.append(("Next", transitions["NextAction"]))

        for err in transitions.get("Errors") or []:
            targets.append((f"Error:{err.get('ErrorType', '?')}", err.get("NextAction")))

        for cond in transitions.get("Conditions") or []:
            targets.append(("Condition", cond.get("NextAction")))

        if not targets and atype not in TERMINAL:
            dangling.append((aid, atype, "no outbound transition"))

        for label, target in targets:
            if not target:
                dangling.append((aid, atype, f"{label} branch is empty"))
            elif target not in ids:
                broken.append((aid, atype, f"{label} -> {target} does not exist"))

    name = flow.get("Metadata", {}).get("name", path.stem)
    print(f"Flow       : {name}")
    print(f"File       : {path}")
    print(f"Actions    : {len(actions)}")
    print(f"Start      : {flow.get('StartAction')}")
    print(f"Dangling   : {len(dangling)}")
    print(f"Broken ref : {len(broken)}")

    for aid, atype, why in dangling:
        print(f"  DANGLING  {aid:<28} {atype:<32} {why}")
    for aid, atype, why in broken:
        print(f"  BROKEN    {aid:<28} {atype:<32} {why}")

    if flow.get("StartAction") and flow["StartAction"] not in ids:
        print(f"  BROKEN    StartAction {flow['StartAction']} does not exist")
        return 1

    return 1 if (dangling or broken) else 0


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2

    worst = 0
    for arg in sys.argv[1:]:
        path = pathlib.Path(arg)
        if not path.exists():
            print(f"not found: {path}")
            worst = 2
            continue
        worst = max(worst, audit(path))
        print()

    return worst


if __name__ == "__main__":
    sys.exit(main())
