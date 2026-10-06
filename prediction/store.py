"""Thread-safe local storage for prospective, shadow-only futures forecasts.

Bars are immutable by instrument and market end time. A historical bar may be
promoted to live provenance without changing its price. Forecasts and realized
evaluations are append-only, and delayed history alone never scores a forecast.
Provisional EWMA bands have their own explicit family and calibration records;
they never impersonate fitted GARCH/Markov forecasts or a Markov state estimate.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import sqlite3
from threading import RLock


UTC = timezone.utc
MODELS = ("garch", "markov")
HORIZONS = (15, 30)


def _forecast_session(payload):
    """Keep undeclared legacy sessions distinct from explicit all-hours data."""
    present = [key in payload for key in ("session_scope", "session_kind")]
    if not any(present):
        return "LEGACY"
    if not all(present):
        raise ValueError("Forecast session_scope and session_kind must be declared together")
    if payload["session_scope"] != "ALL_HOURS" or payload["session_kind"] not in ("CASH", "EXTENDED"):
        raise ValueError("Forecast session_scope must be ALL_HOURS and session_kind must be CASH or EXTENDED")
    return payload["session_kind"]


def _forecast_models(payload):
    """Validate explicit family declarations without rewriting legacy payloads."""
    metadata = ("forecast_mode", "model_family", "available_models")
    present = [key in payload for key in metadata]
    if not any(present):
        families = MODELS  # Existing immutable forecasts predate mode metadata.
    else:
        if not all(present):
            raise ValueError("Forecast mode, model_family, and available_models must be declared together")
        mode, family, available = (payload[key] for key in metadata)
        if mode == "PROVISIONAL" and family == "EWMA" and available == ["ewma"]:
            families = ("ewma",)
            if "high_vol_probability" not in payload or payload["high_vol_probability"] is not None:
                raise ValueError("Provisional EWMA forecasts require high_vol_probability=null; no Markov state is available")
        elif mode == "FITTED" and family == "GARCH_MARKOV" and available == ["garch", "markov"]:
            families = MODELS
            if "high_vol_probability" in payload:
                probability = payload["high_vol_probability"]
                if type(probability) not in (int, float) or not math.isfinite(probability) or not 0 <= probability <= 1:
                    raise ValueError("Fitted high_vol_probability must be a finite probability")
        else:
            raise ValueError("Unsupported forecast mode/model_family/available_models combination")
    for unsupported in set((*MODELS, "ewma")) - set(families):
        if any(f"{unsupported}_{horizon}_{part}" in payload
               for horizon in HORIZONS for part in ("lower", "center", "upper")):
            raise ValueError(f"Forecast declares unavailable {unsupported} band fields")
    return families


def _datetime(value) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Timestamps must include a timezone")
    return parsed.astimezone(UTC)


def _utc(value) -> str:
    return _datetime(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _positive(value, name: str) -> float:
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise ValueError(f"{name} must be finite and positive")
    return result


def _instrument(value) -> str:
    result = str(value).strip()
    if not result or any(ord(c) < 32 for c in result):
        raise ValueError("A full instrument identifier is required")
    return result


class PredictionStore:
    """One guarded SQLite connection; safe for a threaded local service.

    ``clock`` is a test seam. Production should use the default system UTC
    clock, so a backdated payload cannot create a prospective forecast record.
    """

    def __init__(self, path, *, clock=None):
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock = RLock()
        self._closed = False
        self._connection = sqlite3.connect(self.path, timeout=20, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        try:
            tables = {r[0] for r in self._connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables - {"bars", "forecasts", "realized_evaluations", "sqlite_sequence"}:
                raise ValueError("PredictionStore requires its own database; refusing unrelated existing tables")
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.executescript("""
                CREATE TABLE IF NOT EXISTS bars (
                    instrument TEXT NOT NULL,
                    end_utc TEXT NOT NULL,
                    close REAL NOT NULL CHECK(close > 0),
                    source TEXT NOT NULL CHECK(source IN ('history', 'live')),
                    first_received_at_utc TEXT NOT NULL,
                    live_received_at_utc TEXT,
                    PRIMARY KEY(instrument, end_utc)
                );
                CREATE TABLE IF NOT EXISTS forecasts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    instrument TEXT NOT NULL,
                    origin_utc TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    generated_at_utc TEXT NOT NULL,
                    valid_until_utc TEXT NOT NULL,
                    created_at_utc TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    UNIQUE(instrument, origin_utc, model_id)
                );
                CREATE TABLE IF NOT EXISTS realized_evaluations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    forecast_id INTEGER NOT NULL REFERENCES forecasts(id),
                    instrument TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    model TEXT NOT NULL,
                    horizon_minutes INTEGER NOT NULL,
                    endpoint_utc TEXT NOT NULL,
                    endpoint_received_at_utc TEXT NOT NULL,
                    evaluated_at_utc TEXT NOT NULL,
                    origin_price REAL NOT NULL,
                    actual_close REAL NOT NULL,
                    lower REAL NOT NULL,
                    center REAL NOT NULL,
                    upper REAL NOT NULL,
                    nominal_coverage REAL NOT NULL,
                    error REAL NOT NULL,
                    absolute_error REAL NOT NULL,
                    squared_error REAL NOT NULL,
                    covered INTEGER NOT NULL,
                    interval_width REAL NOT NULL,
                    interval_score REAL NOT NULL,
                    UNIQUE(forecast_id, model, horizon_minutes)
                );
                CREATE INDEX IF NOT EXISTS forecast_instrument_origin ON forecasts(instrument, origin_utc);
                CREATE INDEX IF NOT EXISTS evaluation_group ON realized_evaluations(instrument, model, horizon_minutes);
                CREATE TRIGGER IF NOT EXISTS forecasts_no_update BEFORE UPDATE ON forecasts
                    BEGIN SELECT RAISE(ABORT, 'forecasts are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS forecasts_no_delete BEFORE DELETE ON forecasts
                    BEGIN SELECT RAISE(ABORT, 'forecasts are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS evaluations_no_update BEFORE UPDATE ON realized_evaluations
                    BEGIN SELECT RAISE(ABORT, 'evaluations are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS evaluations_no_delete BEFORE DELETE ON realized_evaluations
                    BEGIN SELECT RAISE(ABORT, 'evaluations are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS bars_no_price_update BEFORE UPDATE OF instrument, end_utc, close ON bars
                    BEGIN SELECT RAISE(ABORT, 'bar identity and price are immutable'); END;
            """)
        except Exception:
            self._connection.close()
            self._closed = True
            raise

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        with self._lock:
            if not self._closed:
                self._connection.close()
                self._closed = True

    def upsert_bars(self, instrument, bars, source, received_at):
        """Atomically insert bars, reject price conflicts, and preserve first receipt.

        The caller must validate live feed age/session provenance before using
        ``source='live'``. A repeated live delivery never resets first-live time.
        """
        instrument = _instrument(instrument)
        if source not in ("history", "live"):
            raise ValueError("source must be history or live")
        received = _utc(received_at)
        normalized, duplicates = {}, 0
        for bar in bars:
            end, close = _utc(bar["end_utc"]), _positive(bar["close"], "close")
            if end > received:
                raise ValueError("Cannot receive a completed bar before its market end time")
            if end in normalized:
                if normalized[end] != close:
                    raise ValueError(f"Conflicting prices in batch for {instrument} at {end}")
                duplicates += 1
            normalized[end] = close
        result = {"inserted": 0, "duplicates": duplicates, "upgraded": 0}
        with self._lock, self._connection:
            for end, close in sorted(normalized.items()):
                existing = self._connection.execute("SELECT * FROM bars WHERE instrument=? AND end_utc=?", (instrument, end)).fetchone()
                if existing is None:
                    self._connection.execute(
                        "INSERT INTO bars VALUES (?,?,?,?,?,?)",
                        (instrument, end, close, source, received, received if source == "live" else None),
                    )
                    result["inserted"] += 1
                elif existing["close"] != close:
                    raise ValueError(f"Conflicting stored price for {instrument} at {end}; batch rolled back")
                elif source == "live" and existing["source"] == "history":
                    self._connection.execute("UPDATE bars SET source='live',live_received_at_utc=? WHERE instrument=? AND end_utc=?", (received, instrument, end))
                    result["upgraded"] += 1
                else:
                    result["duplicates"] += 1
        return result

    def get_bars(self, instrument, before=None):
        query, params = "SELECT * FROM bars WHERE instrument=?", [_instrument(instrument)]
        if before is not None:
            query += " AND end_utc<=?"
            params.append(_utc(before))
        with self._lock:
            rows = [dict(r) for r in self._connection.execute(query + " ORDER BY end_utc", params)]
        for row in rows:
            row["received_at_utc"] = row["live_received_at_utc"] or row["first_received_at_utc"]
        return rows

    @staticmethod
    def _forecast(row):
        return dict(json.loads(row["payload_json"]), id=row["id"], created_at_utc=row["created_at_utc"])

    def log_forecast(self, payload):
        """Append a forecast; exact replay returns it, conflicting replay fails."""
        payload = dict(payload)
        payload["instrument"] = _instrument(payload["instrument"])
        payload["model_id"] = str(payload["model_id"]).strip()
        if not payload["model_id"]:
            raise ValueError("model_id is required")
        for key in ("origin_utc", "generated_at_utc", "valid_until_utc"):
            payload[key] = _utc(payload[key])
        if not payload["origin_utc"] <= payload["generated_at_utc"] <= payload["valid_until_utc"]:
            raise ValueError("Require origin <= generated_at <= valid_until")
        payload["origin_price"] = _positive(payload["origin_price"], "origin_price")
        payload["status"] = payload.get("status", "SHADOW")
        if payload["status"] != "SHADOW":
            raise ValueError("Only SHADOW forecasts may be logged")
        payload["nominal_coverage"] = float(payload.get("nominal_coverage", 0.8))
        if not 0 < payload["nominal_coverage"] < 1:
            raise ValueError("nominal_coverage must lie strictly between zero and one")
        _forecast_session(payload)
        families = _forecast_models(payload)
        for model in families:
            for horizon in HORIZONS:
                keys = [f"{model}_{horizon}_{part}" for part in ("lower", "center", "upper")]
                for key in keys:
                    payload[key] = _positive(payload[key], key)
                if not payload[keys[0]] <= payload[keys[1]] <= payload[keys[2]]:
                    raise ValueError(f"Invalid interval order for {model} at {horizon} minutes")
        serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
        key = payload["instrument"], payload["origin_utc"], payload["model_id"]
        with self._lock, self._connection:
            existing = self._connection.execute("SELECT * FROM forecasts WHERE instrument=? AND origin_utc=? AND model_id=?", key).fetchone()
            if existing:
                if existing["payload_json"] != serialized:
                    raise ValueError("Conflicting forecast: instrument/origin/model_id is immutable")
                return self._forecast(existing)
            created = _utc(self._clock())
            self._connection.execute(
                "INSERT INTO forecasts(instrument,origin_utc,model_id,generated_at_utc,valid_until_utc,created_at_utc,payload_json) VALUES (?,?,?,?,?,?,?)",
                (*key, payload["generated_at_utc"], payload["valid_until_utc"], created, serialized),
            )
            return self._forecast(self._connection.execute("SELECT * FROM forecasts WHERE instrument=? AND origin_utc=? AND model_id=?", key).fetchone())

    def get_forecasts(self, instrument=None, limit=100):
        if int(limit) < 1:
            return []
        query, params = "SELECT * FROM forecasts", []
        if instrument is not None:
            query += " WHERE instrument=?"
            params.append(_instrument(instrument))
        params.append(int(limit))
        with self._lock:
            return [self._forecast(r) for r in self._connection.execute(query + " ORDER BY created_at_utc DESC,id DESC LIMIT ?", params)]

    def evaluate(self, instrument=None):
        """Score new exact 15/30-minute endpoints only after prospective live receipt.

        Market end time, first live receipt, declared generation, and actual log
        creation are distinct. All must respect prospective forecast ordering.
        History-only rows, missing endpoints, and later substitute bars do not
        create outcomes. Expiry is kept in the full instrument identifier.
        """
        query, params = "SELECT * FROM forecasts", []
        if instrument is not None:
            query += " WHERE instrument=?"
            params.append(_instrument(instrument))
        new_rows = []
        with self._lock, self._connection:
            forecasts = self._connection.execute(query + " ORDER BY id", params).fetchall()
            for record in forecasts:
                payload = json.loads(record["payload_json"])
                families = _forecast_models(payload)
                eligible_after = max(record["generated_at_utc"], record["created_at_utc"])
                for horizon in HORIZONS:
                    endpoint = _utc(_datetime(record["origin_utc"]) + timedelta(minutes=horizon))
                    if eligible_after > endpoint:
                        continue
                    bar = self._connection.execute("SELECT * FROM bars WHERE instrument=? AND end_utc=? AND source='live'", (record["instrument"], endpoint)).fetchone()
                    if bar is None or bar["live_received_at_utc"] is None or bar["live_received_at_utc"] < eligible_after:
                        continue
                    for model in families:
                        existing = self._connection.execute("SELECT 1 FROM realized_evaluations WHERE forecast_id=? AND model=? AND horizon_minutes=?", (record["id"], model, horizon)).fetchone()
                        if existing:
                            continue
                        lower, center, upper = (payload[f"{model}_{horizon}_{part}"] for part in ("lower", "center", "upper"))
                        actual, nominal = bar["close"], payload["nominal_coverage"]
                        error, width = actual - center, upper - lower
                        score = width + 2 / (1 - nominal) * max(lower - actual, actual - upper, 0)
                        row = {
                            "forecast_id": record["id"], "instrument": record["instrument"], "model_id": record["model_id"],
                            "model": model, "horizon_minutes": horizon, "endpoint_utc": endpoint,
                            "endpoint_received_at_utc": bar["live_received_at_utc"], "evaluated_at_utc": _utc(self._clock()),
                            "origin_price": payload["origin_price"], "actual_close": actual,
                            "lower": lower, "center": center, "upper": upper, "nominal_coverage": nominal,
                            "error": error, "absolute_error": abs(error), "squared_error": error * error,
                            "covered": int(lower <= actual <= upper), "interval_width": width, "interval_score": score,
                        }
                        columns = ",".join(row)
                        placeholders = ",".join("?" for _ in row)
                        cursor = self._connection.execute(f"INSERT INTO realized_evaluations({columns}) VALUES ({placeholders})", tuple(row.values()))
                        new_rows.append(dict(row, id=cursor.lastrowid))
        return new_rows

    def get_evaluations(self, instrument=None, limit=1000):
        query, params = "SELECT * FROM realized_evaluations", []
        if instrument is not None:
            query += " WHERE instrument=?"
            params.append(_instrument(instrument))
        params.append(max(0, int(limit)))
        with self._lock:
            return [dict(r) for r in self._connection.execute(query + " ORDER BY id DESC LIMIT ?", params)]

    def report_metrics(self, instrument=None):
        """Descriptive endpoint calibration, grouped by coverage/version/session.

        These overlapping forecast counts are not independent sample sizes and
        the pointwise coverage statistic is not whole-path or trading accuracy.
        Session labels come from immutable forecasts; unlabelled older records
        remain LEGACY instead of being pooled into either all-hours category.
        """
        query = """SELECT e.instrument,e.model_id,e.model,e.horizon_minutes,e.nominal_coverage,
                          CASE WHEN json_extract(f.payload_json, '$.session_scope') = 'ALL_HOURS'
                                     AND json_extract(f.payload_json, '$.session_kind') IN ('CASH', 'EXTENDED')
                               THEN json_extract(f.payload_json, '$.session_kind')
                               ELSE 'LEGACY' END AS session_kind,
                          COUNT(*) AS count,AVG(e.covered) AS coverage,
                          AVG(e.interval_width) AS mean_width,
                          AVG(e.interval_score) AS mean_interval_score,
                          AVG(e.absolute_error) AS mae,AVG(e.squared_error) AS mse
                   FROM realized_evaluations e JOIN forecasts f ON f.id=e.forecast_id"""
        params = []
        if instrument is not None:
            query += " WHERE e.instrument=?"
            params.append(_instrument(instrument))
        query += (" GROUP BY e.instrument,e.model_id,e.model,e.horizon_minutes,e.nominal_coverage,session_kind"
                  " ORDER BY e.instrument,e.model_id,e.model,e.horizon_minutes,e.nominal_coverage,session_kind")
        with self._lock:
            rows = [dict(r) for r in self._connection.execute(query, params)]
        for row in rows:
            row["rmse"] = math.sqrt(row.pop("mse"))
        return rows

    def status(self, instrument=None):
        where, params = (" WHERE instrument=?", [_instrument(instrument)]) if instrument is not None else ("", [])
        with self._lock:
            result = {name: self._connection.execute(f"SELECT COUNT(*) FROM {name}{where}", params).fetchone()[0]
                      for name in ("bars", "forecasts", "realized_evaluations")}
            latest = self._connection.execute(f"SELECT MAX(end_utc) FROM bars{where}", params).fetchone()[0]
            latest_live_query = "SELECT MAX(end_utc) FROM bars WHERE source='live'" + (" AND instrument=?" if instrument is not None else "")
            result["latest_bar_utc"] = latest
            result["latest_live_bar_utc"] = self._connection.execute(latest_live_query, params).fetchone()[0]
            result["instruments"] = [r[0] for r in self._connection.execute(f"SELECT DISTINCT instrument FROM bars{where} ORDER BY instrument", params)]
        return dict(result, status="SHADOW", calibration_scope="Exact realized 15/30-minute endpoints; overlapping forecasts are dependent.")
