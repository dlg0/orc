"""Tests for orc.events."""

from __future__ import annotations

from datetime import datetime

from orc.events import EventLog, EventType


def test_record_and_read_back(tmp_path) -> None:
    log = EventLog(tmp_path)
    log.record(EventType.issue_selected, {"issue_id": "ISSUE-1"})
    events = log.all()
    assert len(events) == 1
    assert events[0]["event_type"] == "issue_selected"
    assert events[0]["data"] == {"issue_id": "ISSUE-1"}


def test_events_have_timestamps(tmp_path) -> None:
    log = EventLog(tmp_path)
    log.record(EventType.amp_started)
    event = log.all()[0]
    ts = datetime.fromisoformat(event["timestamp"])
    assert ts.year >= 2024


def test_recent_returns_last_n(tmp_path) -> None:
    log = EventLog(tmp_path)
    for i in range(10):
        log.record(EventType.state_changed, {"seq": i})
    recent = log.recent(3)
    assert len(recent) == 3
    assert [e["data"]["seq"] for e in recent] == [7, 8, 9]


def test_append_only(tmp_path) -> None:
    log = EventLog(tmp_path)
    log.record(EventType.amp_started)
    log.record(EventType.amp_finished)
    log.record(EventType.issue_closed)
    events = log.all()
    assert len(events) == 3
    assert events[0]["event_type"] == "amp_started"
    assert events[1]["event_type"] == "amp_finished"
    assert events[2]["event_type"] == "issue_closed"


def test_all_empty_when_no_log(tmp_path) -> None:
    log = EventLog(tmp_path)
    assert log.all() == []
