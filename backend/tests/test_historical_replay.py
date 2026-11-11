from datetime import datetime
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace

from appy import _latest_overview_snapshots
from models import Base, CollectionRun, GexSnapshot, get_engine, get_session_factory


def test_historical_overview_uses_only_snapshots_available_at_cutoff(tmp_path):
    engine = get_engine(tmp_path / "historical-replay.db")
    Base.metadata.create_all(engine)
    Session = get_session_factory(engine)
    with Session.begin() as session:
        run = CollectionRun(started_at=datetime(2026, 10, 1, 9, 30), status="success")
        session.add(run)
        session.flush()
        old = GexSnapshot(collection_run_id=run.id, symbol="SPX", timestamp=datetime(2026, 10, 1, 9, 30),
                          spot_price=6000, total_net_gex=100)
        new = GexSnapshot(collection_run_id=run.id, symbol="SPX", timestamp=datetime(2026, 10, 1, 9, 45),
                          spot_price=6010, total_net_gex=-100)
        session.add_all((old, new))
        session.flush()
        old_id, new_id = old.id, new.id
    with engine.connect() as connection:
        at_930 = _latest_overview_snapshots(connection, {"SPX"}, datetime(2026, 10, 1, 9, 30))
        at_945 = _latest_overview_snapshots(connection, {"SPX"}, datetime(2026, 10, 1, 9, 45))
        assert at_930["SPX"].id == old_id
        assert at_945["SPX"].id == new_id
    engine.dispose()


def test_replay_sampler_reports_malformed_timestamp_without_discarding_session():
    path = Path(__file__).resolve().parents[2] / "scripts" / "replay-scenarios.py"
    spec = spec_from_file_location("replay_scenarios", path)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    rows = [
        SimpleNamespace(id=1, symbol="SPX", timestamp="2026-10-01 09:30:00"),
        SimpleNamespace(id=2, symbol="SPX", timestamp="broken"),
        SimpleNamespace(id=3, symbol="SPX", timestamp="2026-10-01 09:32:00"),
        SimpleNamespace(id=4, symbol="SPX", timestamp="2026-10-01 09:36:00"),
    ]
    selected, invalid = module.sample_rows(rows, 5, 0)
    assert [row.id for row in selected] == [1, 4]
    assert invalid == [{"snapshot_id": 2, "symbol": "SPX", "reason": "invalid stored timestamp"}]
