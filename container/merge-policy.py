"""Merge the containers-policy.json fragment into the system policy.

Usage: merge-policy.py IMAGE_REPO FRAGMENT

Keeps the base image's existing entries and adds (or replaces) the scopes
from the fragment, substituting @IMAGE_REPO@ with the given repository.

Fedora 45's containers-common ships its default policy as
/usr/share/containers/policy.json and no longer creates
/etc/containers/policy.json, so the base policy is read from whichever path
exists. The result is always written to /etc/containers/policy.json, which is
the path the containers stack reads (and which takes precedence over
/usr/share).
"""

import json
import os
import sys

SOURCES = (
    "/etc/containers/policy.json",
    "/usr/share/containers/policy.json",
)
OUTPUT = "/etc/containers/policy.json"

repo, fragment_path = sys.argv[1], sys.argv[2]

source = next((path for path in SOURCES if os.path.exists(path)), None)
if source is None:
    sys.exit("no base policy.json found at " + " or ".join(SOURCES))

with open(source) as f:
    policy = json.load(f)
with open(fragment_path) as f:
    fragment = json.load(f)

transports = policy.setdefault("transports", {})
for transport, scopes in fragment["transports"].items():
    target = transports.setdefault(transport, {})
    for scope, rules in scopes.items():
        target[scope.replace("@IMAGE_REPO@", repo)] = rules

os.makedirs(os.path.dirname(OUTPUT), exist_ok=True)
with open(OUTPUT, "w") as f:
    json.dump(policy, f, indent=4)
    f.write("\n")

print(f"merged {fragment_path} from {source} into {OUTPUT}")
