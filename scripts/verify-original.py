"""Verify the original application's tracked files remain byte-for-byte unchanged."""
import hashlib
import json
from pathlib import Path

root = Path(__file__).resolve().parents[1]
baseline = json.loads((root / "docs/source-baseline.json").read_text())
source = Path(baseline["source"])
changed = []
for name, expected in baseline["files"].items():
    path = source / name
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
        changed.append(name)
if changed:
    print("Original files changed since the baseline: " + ", ".join(changed))
    raise SystemExit(1)
print(f"Original app unchanged: {len(baseline['files'])} tracked files verified.")
