from types import SimpleNamespace

import appy
from market_context import build_market_context
from models import Base, get_engine
from test_market_overview_cache import _seed_overview


def test_missing_event_calendar_is_unavailable_not_normal():
    context = build_market_context('SPX', 100, [], [], None)
    assert context['event_risk']['state'] == 'UNAVAILABLE'
    assert 'Event calendar unavailable' in context['warnings']


def test_workspace_cache_survives_symbol_switches_and_expires(tmp_path, monkeypatch):
    engine = get_engine(tmp_path / 'workspace.db')
    Base.metadata.create_all(engine)
    _seed_overview(engine)
    monkeypatch.setattr(appy, 'engine', engine)
    monkeypatch.setattr(appy, 'DB_SCHEMA_CURRENT', True)
    monkeypatch.setattr(appy, 'get_market_overview', lambda: {})
    clock = [100.0]
    monkeypatch.setattr(appy, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    appy._clear_overview_cache()
    calls = []
    original = appy._dashboard_data_from_engine

    def dashboard(*args, **kwargs):
        calls.append(kwargs.get('snapshot_id'))
        return original(*args, **kwargs)

    monkeypatch.setattr(appy, '_dashboard_data_from_engine', dashboard)
    first = appy.get_decision_workspace('SPX')
    appy.get_decision_workspace('IWM')
    cached = appy.get_decision_workspace('SPX')
    assert len(calls) == 2
    assert cached['snapshot_id'] == first['snapshot_id']
    assert all(snapshot_id is not None for snapshot_id in calls)
    cached['dashboard']['snapshot']['spot_price'] = -1
    assert appy.get_decision_workspace('SPX')['dashboard']['snapshot']['spot_price'] > 0
    clock[0] += 16
    appy.get_decision_workspace('SPX')
    assert len(calls) == 3
    engine.dispose()
