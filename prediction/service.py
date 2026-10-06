"""Standalone localhost service for prospective futures shadow forecasts.

Run ``python -m prediction.service``. No dashboard imports, orders or credentials.
The request/response protocol is documented in docs/predictions.md.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import errno
import hashlib
import json
import logging
from pathlib import Path
import socket
import socketserver
import threading
from uuid import uuid4

from .data import (
    FORECAST_TTL_SECONDS, MAX_LIVE_DELAY_SECONDS, MIN_FIT_RETURNS, MIN_LIVE_CLOSES,
    NEW_YORK, forecast_session_kind, iso_utc, live_closes, normalize_bars, parse_utc,
    training_data, utc_now, validate_instrument,
)
from .models import fit_bundle, forecast_bundle, validate_bundle
from .store import PredictionStore
from .series import SeriesRegistry
from .provisional import provisional_forecast

LOG = logging.getLogger("opengamma.prediction")
DEFAULT_DATA_DIR = Path(__file__).resolve().parents[1] / "prediction_data"
DEFAULT_PORT = 5011
MAX_FRAME_BYTES = 128 * 1024
PROTOCOL_VERSION = 2
MAX_HISTORY_BARS = 20000
HISTORY_POLICIES = {"DoNotMerge", "MergeBackAdjusted", "MergeNonBackAdjusted"}
SERVICE_NAME = "opengamma.prediction"
TRAINING_POLICY = "all-hours-v1"


class PredictionService:
    """Available-history fitting with an explicit EWMA cold-start forecast."""

    def __init__(self, data_dir=DEFAULT_DATA_DIR, *, clock=utc_now, fit=fit_bundle, predict=forecast_bundle, series_metadata=None):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "models").mkdir(exist_ok=True)
        self.clock, self.fit, self.predict = clock, fit, predict
        self.series_metadata = dict(series_metadata or {})
        self.store = PredictionStore(self.data_dir / "forecasts.sqlite3", clock=clock)
        self.lock = threading.RLock()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="prediction-fit")
        self.models, self.jobs, self.failures, self.attempted, self.forecasts = {}, {}, {}, {}, {}
        self.model_evidence, self.job_evidence, self.training_info = {}, {}, {}
        self.latest_live = {}
        self.closed = False

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
        self.executor.shutdown(wait=True, cancel_futures=True)
        with self.lock:
            self.store.close()

    def _require_open(self):
        if self.closed:
            raise RuntimeError("Prediction service is stopping")

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def status(self, instrument, status, message, **details):
        return {"schema_version": PROTOCOL_VERSION, "type": "STATUS", "instrument": instrument,
                "status": status, "message": message, "server_time_utc": iso_utc(self.clock()),
                **self.series_metadata, **details}

    def _key(self, instrument, now):
        return instrument, now.astimezone(NEW_YORK).date()

    def _model_path(self, key):
        contract = hashlib.sha256(key[0].encode()).hexdigest()[:16]
        return self.data_dir / "models" / f"{contract}_{key[1].isoformat()}_{TRAINING_POLICY}.json"

    def _fit_and_save(self, key, blocks, evidence):
        bundle = self.fit(key[0], blocks, evidence["trained_through"])
        validate_bundle(bundle, instrument=key[0])
        envelope = {"forecast_session": key[1].isoformat(), "training": evidence,
                    "training_policy": TRAINING_POLICY,
                    "series": self.series_metadata, "bundle": bundle}
        path = self._model_path(key)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(envelope, allow_nan=False, indent=2), encoding="utf-8")
        temporary.replace(path)
        LOG.info("Fitted %s for %s from %s returns in %s sessions: %s", key[0], key[1], evidence["training_returns"], evidence["sessions"], bundle["model_id"])
        return bundle

    def _harvest(self, key):
        future = self.jobs.get(key)
        if future is not None and future.done():
            del self.jobs[key]
            try:
                self.models[key] = future.result()
                self.model_evidence[key] = self.job_evidence[key]
                self.failures.pop(key, None)
            except Exception as exc:
                LOG.exception("Model fitting failed for %s", key)
                self.failures[key] = str(exc)
            finally:
                self.job_evidence.pop(key, None)

    def _training(self, instrument, now):
        rows = self.store.get_bars(instrument)
        day = now.astimezone(NEW_YORK).date()
        blocks, evidence = training_data(rows, day)
        if evidence["training_returns"] < MIN_FIT_RETURNS:
            # Include only observations strictly before the potential live
            # origin, never that origin's return or a later observation.
            cutoff = self.latest_live.get(instrument)
            if cutoff is None or cutoff.astimezone(NEW_YORK).date() != day:
                cutoff = now.replace(minute=now.minute // 5 * 5, second=0, microsecond=0)
            blocks, evidence = training_data(rows, day, before=cutoff)
        return blocks, evidence

    def ensure_model(self, instrument, now):
        """Nonblocking refit, once per day; retry failures only after data changes."""
        self._require_open()
        key = self._key(instrument, now)
        self._harvest(key)
        if key in self.models or key in self.jobs:
            return
        if key not in self.attempted:
            path = self._model_path(key)
            if path.exists():
                try:
                    saved = json.loads(path.read_text(encoding="utf-8"))
                    bundle = saved["bundle"]
                    validate_bundle(bundle, instrument=instrument)
                    evidence = dict(saved["training"])
                    cutoff = self.latest_live.get(instrument)
                    if cutoff is None or cutoff.astimezone(NEW_YORK).date() != key[1]:
                        cutoff = now.replace(minute=now.minute // 5 * 5, second=0, microsecond=0)
                    if (saved["forecast_session"] != key[1].isoformat()
                            or saved.get("training_policy") != TRAINING_POLICY
                            or not evidence.get("training_end_utc")
                            or parse_utc(evidence["training_end_utc"]) >= cutoff
                            or evidence["training_returns"] < MIN_FIT_RETURNS
                            or saved.get("series", {}) != self.series_metadata):
                        raise ValueError("Stored model does not precede the forecast origin")
                    self.models[key] = bundle
                    self.model_evidence[key] = evidence
                    self.training_info[key] = evidence
                    return
                except (ValueError, KeyError, TypeError, OSError):
                    LOG.warning("Ignoring invalid model artifact %s", path, exc_info=True)
        blocks, evidence = self._training(instrument, now)
        self.training_info[key] = evidence
        if evidence["training_returns"] < MIN_FIT_RETURNS:
            return
        if self.attempted.get(key) == evidence["fingerprint"]:
            return
        self.attempted[key] = evidence["fingerprint"]
        self.failures.pop(key, None)
        self.job_evidence[key] = evidence
        self.jobs[key] = self.executor.submit(self._fit_and_save, key, blocks, evidence)

    def ingest(self, instrument, bars, source):
        if source not in ("history", "live"):
            raise ValueError("Bar source must be history or live")
        with self.lock:
            self._require_open()
            validate_instrument(instrument)
            now = parse_utc(self.clock())
            selected = normalize_bars(bars, now)
            if source == "live":
                # Historical/replay frames cannot qualify as prospective observations.
                if any((now - parse_utc(row["end_utc"])).total_seconds() > MAX_LIVE_DELAY_SECONDS for row in selected):
                    raise ValueError("Live bars arrived more than 90 seconds after their close; reconnect to backfill history")
            if selected:
                self.store.upsert_bars(instrument, selected, source, now)
                if source == "live":
                    stamp = max(parse_utc(row["end_utc"]) for row in selected)
                    previous = self.latest_live.get(instrument)
                    if previous is None or stamp > previous:
                        self.latest_live[instrument] = stamp
                    self.store.evaluate(instrument)
            return len(selected)

    def poll(self, instrument, *, allow_training=True):
        with self.lock:
            self._require_open()
            validate_instrument(instrument)
            now = parse_utc(self.clock())
            key = self._key(instrument, now)
            if allow_training:
                self.ensure_model(instrument, now)
            bundle = self.models.get(key)
            origin = self.latest_live.get(instrument)
            if origin is None:
                if key in self.jobs:
                    return self.status(instrument, "TRAINING", "Fitting available history; provisional bands begin on a fresh five-minute close")
                return self.status(instrument, "WARMING_UP", "Waiting for a fresh completed five-minute futures bar")
            cached = self.forecasts.get(instrument)
            if cached is not None and parse_utc(cached["origin_utc"]) == origin:
                if now <= parse_utc(cached["valid_until_utc"]):
                    return cached
                return self.status(instrument, "STALE", "Waiting for a new completed five-minute bar")
            if not 0 <= (now - origin).total_seconds() <= MAX_LIVE_DELAY_SECONDS:
                return self.status(instrument, "STALE", "No recent completed five-minute bar")
            rows = self.store.get_bars(instrument)
            closes = live_closes(rows, origin)
            if not closes:
                return self.status(instrument, "WARMING_UP", f"Need {MIN_LIVE_CLOSES} consecutive five-minute closes; all hours accepted, gaps reset the filter")
            # Preserve the first issued forecast for an origin even when a
            # background fit finishes or the service restarts. Promotion to a
            # fitted model occurs on the next origin, not by rewriting history.
            for previous in self.store.get_forecasts(instrument, limit=20):
                if (parse_utc(previous["origin_utc"]) == origin
                        and previous.get("series_id") == self.series_metadata.get("series_id")):
                    self.forecasts[instrument] = previous
                    return previous
            evidence = self.model_evidence.get(key, self.training_info.get(key, {}))
            fitted = bundle is not None and evidence.get("training_end_utc") is not None and parse_utc(evidence["training_end_utc"]) < origin
            if fitted:
                prediction = {**self.predict(bundle, closes), "forecast_mode": "FITTED",
                              "model_family": "GARCH_MARKOV", "available_models": ["garch", "markov"]}
                reason = "GARCH and Markov fitted on available history; nominal intervals"
            else:
                try:
                    prediction = provisional_forecast(closes)
                except ValueError as exc:
                    return self.status(instrument, "WARMING_UP", str(exc))
                if key in self.jobs:
                    reason = "EWMA provisional bands; GARCH/Markov fitting in the background"
                elif key in self.failures:
                    reason = "EWMA provisional bands; GARCH/Markov fit unavailable"
                else:
                    reason = "EWMA provisional bands; using available price history"
            parameter_model_id = prediction["model_id"]
            model_id = parameter_model_id + "-" + TRAINING_POLICY
            if self.series_metadata:
                model_id += "-" + hashlib.sha256(self.series_metadata["series_id"].encode()).hexdigest()[:12]
            payload = {
                "schema_version": PROTOCOL_VERSION, "type": "FORECAST", "status": "SHADOW",
                "instrument": instrument, "origin_utc": iso_utc(origin),
                "generated_at_utc": iso_utc(now),
                "valid_until_utc": iso_utc(origin + timedelta(seconds=FORECAST_TTL_SECONDS)),
                "origin_price": closes[-1], "bar_minutes": 5,
                "session_scope": "ALL_HOURS", "session_kind": forecast_session_kind(origin),
                "training_policy": TRAINING_POLICY,
                "trained_through": bundle["trained_through"] if fitted else None,
                "training_end_utc": evidence.get("training_end_utc") if fitted else None,
                "training_returns": evidence.get("training_returns", 0),
                "training_sessions": evidence.get("sessions", 0),
                "observed_closes": len(closes), "mode_reason": reason,
                "calibration_status": "uncalibrated", "interval_method": "gaussian_moment_endpoint",
                "high_vol_horizon_minutes": 5 if fitted else None, **prediction, **self.series_metadata,
                "model_id": model_id, "parameter_model_id": parameter_model_id,
            }
            # Durably record the forecast before it can be sent to any chart.
            payload = self.store.log_forecast(payload)
            self.forecasts[instrument] = payload
            return payload


class PredictionCoordinator:
    """Route a supplied chart series to its own immutable history revision.

    A NinjaTrader contract label does not prove that the provider's historical
    bars came from that individual expiry. Policy and feed labels are provenance,
    not guarantees of unmerged data. Revision selection examines actual prices.
    """

    def __init__(self, data_dir=DEFAULT_DATA_DIR, *, clock=utc_now, fit=fit_bundle, predict=forecast_bundle):
        self.data_dir = Path(data_dir)
        self.registry = SeriesRegistry(self.data_dir)
        self.clock, self.fit, self.predict = clock, fit, predict
        self.lock = threading.RLock()
        self.services, self.current = {}, {}
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            services = list(self.services.values())
        for service in services:
            service.close()

    @staticmethod
    def provenance(metadata):
        return {key: metadata[key] for key in ("series_id", "profile_id", "history_policy", "feed_label", "revision")}

    def status(self, instrument, status, message, *, history_policy="", feed_label="", series_id="", **details):
        return {"schema_version": PROTOCOL_VERSION, "type": "STATUS", "instrument": instrument,
                "history_policy": history_policy, "feed_label": feed_label, "series_id": series_id,
                "status": status, "message": message, "server_time_utc": iso_utc(self.clock()), **details}

    def commit_history(self, instrument, policy, label, history):
        with self.lock:
            if self.closed:
                raise RuntimeError("Prediction service is stopping")
            metadata = self.registry.select(instrument, policy, label, history, self.clock())
            if not metadata["is_current"]:
                raise ValueError("This chart supplied a retired history revision. Reload its historical data to match the current feed; use a new Feed Label for an intentional provider reversion.")
            series_id = metadata["series_id"]
            # Registry selection persists the new current revision. Retire an
            # old connection immediately, even if opening/ingesting the new
            # store subsequently fails and the new chart must retry.
            self.current[metadata["profile_id"]] = series_id
            service = self.services.get(series_id)
            if service is None:
                service = PredictionService(metadata["data_dir"], clock=self.clock, fit=self.fit,
                                            predict=self.predict, series_metadata=self.provenance(metadata))
                self.services[series_id] = service
            # No mixing with older revisions: a newly selected directory starts
            # with only this complete snapshot. This transaction cannot alter an
            # already issued forecast or an old bar's price.
            service.store.upsert_bars(instrument, history, "history", self.clock())
            response = service.poll(instrument)
            if response["type"] == "FORECAST":
                response = service.status(instrument, "WARMING_UP", "History ready; requesting the current shadow forecast")
            return service, response

    def _require_current(self, service):
        if self.closed:
            raise RuntimeError("Prediction service is stopping")
        meta = service.series_metadata
        if self.current.get(meta["profile_id"]) != meta["series_id"]:
            raise ValueError("Chart history was revised by another connection. Reload historical data before reconnecting.")

    def poll(self, service, instrument):
        with self.lock:
            self._require_current(service)
            return service.poll(instrument)

    def ingest(self, service, instrument, bars):
        with self.lock:
            self._require_current(service)
            service.ingest(instrument, bars, "live")
            return service.poll(instrument)

    def report(self):
        result = []
        for metadata in self.registry.list_series():
            path = Path(metadata["data_dir"]) / "forecasts.sqlite3"
            if path.exists():
                with PredictionStore(path, clock=self.clock) as store:
                    result.append({**self.provenance(metadata), "instrument": metadata["instrument"],
                                   "is_current": metadata["is_current"],
                                   "status": store.status(), "metrics": store.report_metrics()})
        legacy = self.data_dir / "forecasts.sqlite3"
        if legacy.exists():
            with PredictionStore(legacy, clock=self.clock) as store:
                result.append({"series_id": "legacy-unscoped", "history_policy": "legacy-unverified",
                               "is_current": False, "status": store.status(), "metrics": store.report_metrics()})
        return {"series": result}


class ForecastServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, service, port=DEFAULT_PORT):
        self.service = service
        self.slots = threading.BoundedSemaphore(8)
        self.connection_lock = threading.Lock()
        self.connections = set()
        super().__init__(("127.0.0.1", port), ForecastHandler)

    def get_request(self):
        request, address = super().get_request()
        with self.connection_lock:
            self.connections.add(request)
        return request, address

    def shutdown_request(self, request):
        with self.connection_lock:
            self.connections.discard(request)
        super().shutdown_request(request)

    def server_close(self):
        # Wake readers before the coordinator closes its store/executor.
        with self.connection_lock:
            connections = list(self.connections)
        for request in connections:
            try:
                request.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            request.close()
        super().server_close()

    def verify_request(self, request, client_address):
        return self.slots.acquire(blocking=False)

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


class ForecastHandler(socketserver.StreamRequestHandler):
    def handle(self):
        self.request.settimeout(20)
        instrument, history_complete, history_failed = None, False, False
        policy, label, series_id = "", "", ""
        history, service = {}, None
        coordinator = self.server.service

        def status(code, message, **details):
            return coordinator.status(instrument, code, message, history_policy=policy,
                                      feed_label=label, series_id=series_id, **details)

        try:
            while True:
                raw = self.rfile.readline(MAX_FRAME_BYTES + 1)
                if not raw:
                    return
                if len(raw) > MAX_FRAME_BYTES or not raw.endswith(b"\n"):
                    return
                kind, source = None, None
                try:
                    frame = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Nonfinite JSON")))
                    if (not isinstance(frame, dict) or type(frame.get("schema_version")) is not int
                            or frame["schema_version"] != PROTOCOL_VERSION):
                        raise ValueError("Expected protocol schema_version 2; update both the indicator and forecast service")
                    kind = frame.get("type")
                    if kind == "HELLO":
                        if instrument is not None:
                            raise ValueError("Only one HELLO is allowed per connection")
                        candidate = validate_instrument(frame.get("instrument"), frame.get("symbol"))
                        if (frame.get("mode") != "live" or type(frame.get("bar_minutes")) is not int
                                or frame["bar_minutes"] != 5):
                            raise ValueError("Only live five-minute futures feeds are supported")
                        policy = frame.get("history_policy")
                        if policy not in HISTORY_POLICIES:
                            raise ValueError("Send the resolved NinjaTrader merge policy")
                        label = frame.get("feed_label")
                        if (not isinstance(label, str) or not 1 <= len(label) <= 64 or label != label.strip()
                                or any(ord(c) < 32 for c in label)):
                            raise ValueError("Feed Label must contain 1–64 characters with no surrounding whitespace or control characters")
                        instrument = candidate
                        LOG.info("Prediction chart connected: %s; policy=%s; feed=%s", instrument, policy, label)
                        response = status("WAITING_FOR_HISTORY", "Connected; upload the chart's completed history then HISTORY_END",
                                          service_name=SERVICE_NAME, data_dir=str(coordinator.data_dir.resolve()),
                                          session_scope="ALL_HOURS", training_policy=TRAINING_POLICY)
                    elif instrument is None:
                        raise ValueError("Send HELLO first")
                    elif kind == "BAR_BATCH":
                        source = frame.get("source")
                        if source == "live" and not history_complete:
                            raise ValueError("Send HISTORY_END before live bars")
                        if source == "history" and history_complete:
                            raise ValueError("Reconnect before uploading another history snapshot")
                        if source == "live":
                            response = coordinator.ingest(service, instrument, frame.get("bars"))
                        elif source == "history":
                            selected = normalize_bars(frame.get("bars"), coordinator.clock())
                            for row in selected:
                                previous = history.get(row["end_utc"])
                                if previous is not None and previous["close"] != row["close"]:
                                    raise ValueError("Conflicting prices within this history upload; reload and reconnect")
                            if len(set(history) | {r["end_utc"] for r in selected}) > MAX_HISTORY_BARS:
                                raise ValueError("History snapshot exceeds 20,000 five-minute bars")
                            history.update((row["end_utc"], row) for row in selected)
                            response = status("WAITING_FOR_HISTORY", "Staging chart history", accepted_bars=len(selected))
                        else:
                            raise ValueError("Bar source must be history or live")
                    elif kind == "HISTORY_END":
                        if history_complete:
                            raise ValueError("History has already been committed for this connection")
                        if history_failed:
                            raise ValueError("History upload had errors; reconnect and upload a complete snapshot")
                        service, response = coordinator.commit_history(instrument, policy, label,
                                                                        sorted(history.values(), key=lambda r: r["end_utc"]))
                        series_id = service.series_metadata["series_id"]
                        history_complete = True
                        history.clear()
                    elif kind == "PING":
                        response = (coordinator.poll(service, instrument) if history_complete else
                                    status("WAITING_FOR_HISTORY", "Complete history upload first"))
                    else:
                        raise ValueError("Unknown request type")
                except (ValueError, KeyError, TypeError, OverflowError) as exc:
                    if kind == "BAR_BATCH" and source == "history":
                        history_failed = True
                    if kind == "HELLO":
                        LOG.warning("Prediction HELLO rejected: instrument=%r, symbol=%r; %s",
                                    str(frame.get("instrument"))[:80], str(frame.get("symbol"))[:20], exc)
                    response = status("ERROR", str(exc))
                except Exception:
                    LOG.exception("Forecast request failed for %s", instrument)
                    response = status("ERROR", "Prediction service error; inspect its log")
                self.wfile.write((json.dumps(response, allow_nan=False, separators=(",", ":")) + "\n").encode())
                self.wfile.flush()
        except (OSError, socket.timeout):
            return


def probe_existing_service(port, *, timeout=1.0):
    """Recognize an existing server without uploading or committing any data.

    HELLO also works with older schema-2 instances that have no service identity
    fields. Their data directory remains explicitly unknown to the caller.
    """
    label = "LauncherProbe-" + uuid4().hex
    hello = {"schema_version": PROTOCOL_VERSION, "type": "HELLO", "instrument": "ES 12-26",
             "symbol": "ES", "bar_minutes": 5, "mode": "live",
             "history_policy": "MergeBackAdjusted", "feed_label": label}
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as client:
            with client.makefile("rwb") as stream:
                stream.write((json.dumps(hello) + "\n").encode("utf-8"))
                stream.flush()
                raw = stream.readline(MAX_FRAME_BYTES + 1)
        if len(raw) > MAX_FRAME_BYTES or not raw.endswith(b"\n"):
            return None
        response = json.loads(raw)
        if not isinstance(response, dict) or any(response.get(key) != value for key, value in {
            "schema_version": PROTOCOL_VERSION, "type": "STATUS", "status": "WAITING_FOR_HISTORY",
            "instrument": hello["instrument"], "history_policy": hello["history_policy"],
            "feed_label": label,
            "message": "Connected; upload the chart's completed history then HISTORY_END",
        }.items()):
            return None
        parse_utc(response["server_time_utc"])
        if "service_name" not in response and "data_dir" not in response:
            return {"data_dir": None}
        directory = response.get("data_dir")
        if (response.get("service_name") != SERVICE_NAME or not isinstance(directory, str)
                or not directory or not Path(directory).is_absolute()):
            return None
        return {"data_dir": directory}
    except (OSError, ValueError, KeyError, TypeError):
        return None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--report", action="store_true", help="Print recorded shadow accuracy and service data counts, then exit")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    with PredictionCoordinator(args.data_dir) as service:
        if args.report:
            print(json.dumps(service.report(), indent=2, allow_nan=False))
            return 0
        try:
            server = ForecastServer(service, args.port)
        except OSError as exc:
            if exc.errno != errno.EADDRINUSE and getattr(exc, "winerror", None) != 10048:
                LOG.error("Cannot start forecast service on 127.0.0.1:%s: %s", args.port, exc)
                return 1
            existing = probe_existing_service(args.port)
            if existing is None:
                LOG.error("Port %s is already in use, but a responding OpenGamma prediction service could not be verified. "
                          "Close the application using that port, or launch with -Port 5012 and set the same Prediction Port in NinjaTrader.", args.port)
                return 1
            directory = existing["data_dir"]
            if directory is None:
                if args.data_dir.resolve() != DEFAULT_DATA_DIR.resolve():
                    LOG.error("An older OpenGamma prediction service is already running on port %s, but its data folder cannot be verified. "
                              "Stop that instance before starting with --data-dir %s.", args.port, args.data_dir)
                    return 1
                LOG.warning("The running service uses an older startup protocol; its data folder could not be verified.")
            elif Path(directory).resolve() != args.data_dir.resolve():
                LOG.error("OpenGamma prediction service is already running on port %s with a different data folder: %s. "
                          "Stop that instance or choose a different port before using %s.", args.port, directory, args.data_dir)
                return 1
            LOG.info("OpenGamma prediction service is already running on 127.0.0.1:%s. "
                     "No second instance was started. You can close this launcher window; the existing service stays running.", args.port)
            return 0
        with server:
            LOG.info("OpenGamma shadow forecasts listening on 127.0.0.1:%s; data: %s", args.port, args.data_dir)
            LOG.info("All-hours forecasts enabled. Waiting for NinjaTrader: six consecutive five-minute closes start provisional bands; no minimum day count. Extended hours are experimental.")
            try:
                server.serve_forever(poll_interval=0.25)
            except KeyboardInterrupt:
                LOG.info("Stopping forecast service")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
