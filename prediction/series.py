"""Durable chart-data identities without rewriting old bars or forecasts.

A profile is an exact instrument, declared history policy, and feed label.
Those declarations are metadata, not proof of how a provider merged history.
Within a profile, corrections and disjoint histories create distinct revisions.
Recognized retired histories are returned as retired, rather than alternating
the current revision when stale charts reconnect. An intentional return to an
old provider basis requires a new feed label/version.

The coordinator must serialize select + complete history ingestion. This module
only reads existing bar databases and never imports or mutates PredictionStore.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
from threading import RLock
import unicodedata
from uuid import uuid4


HISTORY_POLICIES = frozenset({"DoNotMerge", "MergeBackAdjusted", "MergeNonBackAdjusted"})
_PROFILE = re.compile(r"[0-9a-f]{64}\Z")
_REVISION = re.compile(r"r([0-9]{6,})\Z")


def _text(value, name, maximum):
    if not isinstance(value, str) or any(unicodedata.category(c) == "Cc" for c in value):
        raise ValueError(f"{name} must be a string without control characters")
    result = value.strip()
    if not 1 <= len(result) <= maximum:
        raise ValueError(f"{name} must contain 1–{maximum} characters after trimming")
    return result


def _identity(instrument, history_policy, feed_label):
    instrument = _text(instrument, "instrument", 128)
    if not isinstance(history_policy, str) or history_policy not in HISTORY_POLICIES:
        raise ValueError("Unsupported history_policy")
    return {"instrument": instrument, "history_policy": history_policy,
            "feed_label": _text(feed_label, "feed_label", 64)}


def _encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def _profile_id(identity):
    return hashlib.sha256(_encoded(identity)).hexdigest()


def _utc(value):
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("History and receipt timestamps must include a UTC offset")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _history(history):
    normalized = {}
    for bar in history:
        stamp = _utc(bar["end_utc"])
        if isinstance(bar["close"], bool):
            raise ValueError("History prices must be finite and positive")
        price = float(bar["close"])
        if not math.isfinite(price) or price <= 0:
            raise ValueError("History prices must be finite and positive")
        if stamp in normalized and normalized[stamp] != price:
            raise ValueError("Incoming history contains conflicting duplicate timestamps")
        normalized[stamp] = price
    return dict(sorted(normalized.items()))


class SeriesRegistry:
    def __init__(self, root):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.series_root = self._contained(self.root / "series")
        self.series_root.mkdir(exist_ok=True)
        self._lock = RLock()

    def _contained(self, path):
        resolved = Path(path).resolve()
        if not resolved.is_relative_to(self.root):
            raise ValueError("Series path escapes the registry root")
        return resolved

    def _atomic_json(self, path, payload, *, immutable=False):
        path = self._contained(path)
        if immutable and path.exists():
            raise ValueError("Revision metadata is immutable")
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as stream:
                stream.write(_encoded(payload) + b"\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()

    def _records(self, profile_path, expected_identity=None):
        """Recover complete immutable revisions even after a pointer-write crash."""
        records = []
        if not profile_path.exists():
            return records
        profile_path = self._contained(profile_path)
        for directory in sorted(profile_path.iterdir()):
            match = _REVISION.fullmatch(directory.name)
            if match is None or not directory.is_dir():
                continue
            metadata_path = self._contained(directory / "series.json")
            if not metadata_path.exists():
                continue  # An interrupted mkdir before the atomic metadata write.
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            if not isinstance(metadata, dict) or metadata.get("schema_version") != 1:
                raise ValueError(f"Unsupported series metadata: {metadata_path}")
            identity = _identity(metadata["instrument"], metadata["history_policy"], metadata["feed_label"])
            profile_id = _profile_id(identity)
            revision = metadata.get("revision")
            if (type(revision) is not int or revision < 1 or revision != int(match[1])
                    or directory.name != f"r{revision:06d}"
                    or metadata.get("profile_id") != profile_id or profile_path.name != profile_id
                    or metadata.get("series_id") != f"{profile_id}:r{revision:06d}"):
                raise ValueError(f"Invalid series identity or revision: {metadata_path}")
            if expected_identity is not None and identity != expected_identity:
                raise ValueError("Profile metadata does not match the selected chart identity")
            records.append(dict(metadata, data_dir=self._contained(directory)))
        records.sort(key=lambda row: row["revision"])
        return records

    def _write_manifest(self, profile_path, identity, revision):
        self._atomic_json(profile_path / "manifest.json", {
            "schema_version": 1, "profile_id": _profile_id(identity), **identity,
            "current_revision": revision,
        })

    def _check_manifest(self, profile_path, identity, records):
        path = self._contained(profile_path / "manifest.json")
        expected = records[-1]["revision"] if records else None
        if not path.exists():
            if records:
                self._write_manifest(profile_path, identity, expected)
            return
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(manifest, dict) or manifest.get("schema_version") != 1
                or manifest.get("profile_id") != _profile_id(identity)
                or any(manifest.get(k) != v for k, v in identity.items())
                or type(manifest.get("current_revision")) is not int):
            raise ValueError("Invalid series profile manifest")
        revisions = {row["revision"] for row in records}
        if manifest["current_revision"] not in revisions:
            raise ValueError("Profile manifest refers to missing revision metadata")
        if manifest["current_revision"] != expected:
            # Metadata is persisted first. A newer complete revision wins when
            # a crash interrupted the subsequent manifest pointer replacement.
            self._write_manifest(profile_path, identity, expected)

    def _bars(self, record):
        database = self._contained(record["data_dir"] / "forecasts.sqlite3")
        if not database.exists():
            return {}
        wal = self._contained(Path(str(database) + "-wal"))
        # Immutable reads avoid new sidecars only when there is no WAL content.
        immutable = not wal.exists() or wal.stat().st_size == 0
        uri = database.as_uri() + "?mode=ro" + ("&immutable=1" if immutable else "")
        connection = sqlite3.connect(uri, uri=True, timeout=10)
        try:
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "bars" not in tables:
                if tables:
                    raise ValueError("Series database has no compatible bars table")
                return {}
            foreign = connection.execute("SELECT 1 FROM bars WHERE instrument<>? LIMIT 1", (record["instrument"],)).fetchone()
            if foreign:
                raise ValueError("Series database contains a different instrument")
            rows = connection.execute("SELECT end_utc,close FROM bars WHERE instrument=? ORDER BY end_utc", (record["instrument"],)).fetchall()
            return _history({"end_utc": stamp, "close": price} for stamp, price in rows)
        finally:
            connection.close()

    @staticmethod
    def _comparison(existing, incoming):
        overlap = existing.keys() & incoming.keys()
        conflicts = {stamp for stamp in overlap if existing[stamp] != incoming[stamp]}
        return overlap, conflicts

    @staticmethod
    def _selection(record, is_current, reason, incoming_fingerprint, overlap=0, conflicts=0):
        return dict(record, is_current=is_current, selection_reason=reason,
                    selection_history_fingerprint=incoming_fingerprint,
                    overlapping_bars=overlap, conflicting_bars=conflicts)

    def select(self, instrument, history_policy, feed_label, history, received_at):
        """Select a basis before the coordinator ingests the full snapshot.

        ``history_fingerprint`` describes the snapshot which CREATED the
        revision; it never changes. ``selection_history_fingerprint`` describes
        this invocation. Empty histories attach only to this profile's current
        revision. An ``is_current=False`` result must be rejected by the caller,
        without ingesting it or publishing forecasts from that retired basis.
        """
        identity = _identity(instrument, history_policy, feed_label)
        incoming, received = _history(history), _utc(received_at)
        if any(stamp > received for stamp in incoming):
            raise ValueError("Cannot select history containing unfinished future bars")
        fingerprint = hashlib.sha256(_encoded(list(incoming.items()))).hexdigest()
        profile_id = _profile_id(identity)
        with self._lock:
            profile_path = self._contained(self.series_root / profile_id)
            profile_path.mkdir(exist_ok=True)
            records = self._records(profile_path, identity)
            self._check_manifest(profile_path, identity, records)
            reason, overlap, conflicts = "new_profile", set(), set()
            if records:
                current = records[-1]
                existing = self._bars(current)
                overlap, conflicts = self._comparison(existing, incoming)
                if not incoming:
                    return self._selection(current, True, "empty_history_attachment", fingerprint)
                if conflicts:
                    for retired in reversed(records[:-1]):
                        old = self._bars(retired)
                        old_overlap, old_conflicts = self._comparison(old, incoming)
                        confirmed_old = any(stamp in old and old[stamp] == incoming[stamp] for stamp in conflicts)
                        if old_overlap and not old_conflicts and confirmed_old:
                            return self._selection(retired, False, "retired_history_basis", fingerprint, len(overlap), len(conflicts))
                    reason = "overlapping_price_correction"
                elif existing and not overlap:
                    reason = "nonoverlapping_history"
                else:
                    return self._selection(current, True, "compatible_history", fingerprint, len(overlap))
            revision = records[-1]["revision"] + 1 if records else 1
            directory = self._contained(profile_path / f"r{revision:06d}")
            directory.mkdir(exist_ok=True)
            metadata = {
                "schema_version": 1, "profile_id": profile_id,
                "series_id": f"{profile_id}:r{revision:06d}", **identity,
                "revision": revision, "created_at_utc": received,
                "creation_reason": reason,
                "history_fingerprint": fingerprint,
                "initial_history_bars": len(incoming),
                "initial_history_start_utc": min(incoming) if incoming else None,
                "initial_history_end_utc": max(incoming) if incoming else None,
                "history_policy_scope": "Declared chart metadata only; provider merging and adjustment are not independently verified.",
            }
            self._atomic_json(directory / "series.json", metadata, immutable=True)
            self._write_manifest(profile_path, identity, revision)
            return self._selection(dict(metadata, data_dir=directory), True, reason, fingerprint, len(overlap), len(conflicts))

    def list_series(self):
        """Return every complete immutable revision, with its current flag."""
        with self._lock:
            result = []
            for directory in sorted(self.series_root.iterdir()):
                if not directory.is_dir() or _PROFILE.fullmatch(directory.name) is None:
                    continue
                records = self._records(self._contained(directory))
                if records:
                    current = records[-1]["revision"]
                    result.extend(dict(record, is_current=record["revision"] == current) for record in records)
            return result
