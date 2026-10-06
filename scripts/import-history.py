"""Copy historical SQLite data without editing the source. New app must be closed."""
import argparse
import json
from pathlib import Path
import sqlite3


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="Existing SQLite database to copy read-only")
    args = parser.parse_args()
    source = args.source.expanduser().resolve(strict=True)
    project = Path(__file__).resolve().parents[1]
    destination = project / "data" / "gex_data.db"
    if source == destination:
        parser.error("The source must differ from the new workspace database.")
    if destination.exists():
        parser.error("The new workspace already has a database. Move it aside with the app closed before importing.")
    if (source.parent / "SYNTHETIC_DEMO.json").exists():
        parser.error("Synthetic demo history cannot be imported into a live workspace.")
    destination.parent.mkdir(exist_ok=True)
    # SQLite backup includes committed WAL contents and never opens the source writable.
    with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as reader:
        reader.execute("PRAGMA query_only=ON")
        if not reader.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='gex_snapshots'").fetchone():
            parser.error("The source is not a PublicGex history database.")
        with sqlite3.connect(destination) as writer:
            reader.backup(writer, pages=256)
    (destination.parent / ".publicgex-workspace.json").write_text(json.dumps({"application_id": "com.publicgex.dashboard.isolated", "version": 1, "mode": "live"}, indent=2), encoding="utf-8")
    print(f"Copied historical data to {destination}. The source was opened read-only.")
    print("Credentials and settings were not imported. The new app applies migrations to its own copy on startup.")


if __name__ == "__main__":
    main()
