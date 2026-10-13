"""Full-publication recovery and its cursor, ownership and capacity boundaries."""
import json
import threading

import pytest

from collector.record import Collected
from runtime import replay as replay_module
from runtime.replay import CursorError, Replay
from runtime.sampler import Sampler
from runtime.slow import Slow

S = 10**9


class Clock:
    ns = 0

    def __call__(self):
        return self.ns


def setup(groups=(), **options):
    clock = Clock()
    sampler = Sampler(lambda: {"cpu": [], "nested": {"value": None}}, clock=(clock, {"source": "fake"}))
    sampler.replay = Replay(sampler.instance_id, clock, sampler.clock, groups, **options)
    return sampler, clock, sampler.replay


def test_full_snapshot_provenance_and_stale_values_are_recoverable():
    sampler, clock, replay = setup()

    def collect():
        stats = {"cpu": [{"id": 0, "freq": 1000, "usage": 0}], "power": {"rail": 0},
                 "network": {"interfaces": [], "read": None}, "arbitrary": {"nested": [None, False, 0]}}
        sensors = {ptr: {"id": ptr, "provider": "fake", "unit": "test", "identity_basis": "test", "span": (0, 0)}
                   for ptr in ("/cpu/0/freq", "/power/rail")}
        return Collected(stats, sensors, dict.fromkeys(("cpu", "memory", "disk", "uptime"), (0, 0)), clock)
    sampler.collect = collect
    sampler._attempt()
    live = sampler.read()[0]
    replayed = replay.view("core")["records"][0]["data"]
    assert {k: v for k, v in replayed.items() if k != "sample"} == {k: v for k, v in live.items() if k != "sample"}
    assert replayed["sample"]["sequence"] == live["sample"]["sequence"]
    clock.ns = 6 * S
    assert sampler.read()[0] is None
    page = replay.view("core")
    assert page["historical"] and page["records"][0]["data"]["sample"]["stale"]
    assert page["records"][0]["data"]["sample"]["data_age_ms"] == 6000
    assert page["records"][0]["data"]["power"]["rail"] == 0


def test_pages_survive_new_publications_and_have_no_duplicates():
    sampler, clock, replay = setup()
    for _ in range(5):
        sampler._attempt()
        clock.ns += S
    first = replay.view("core", limit=2)
    assert [r["id"] for r in first["records"]] == [1, 2]
    assert first["has_more"] and first["next_after"] == 2
    sampler._attempt()
    second = replay.view("core", sampler.instance_id, first["next_after"], 2)
    third = replay.view("core", sampler.instance_id, second["next_after"], 2)
    assert [r["id"] for r in second["records"] + third["records"]] == [3, 4, 5, 6]
    assert not third["has_more"] and third["missing_ranges"] == []
    assert replay.view("core", sampler.instance_id, 6)["records"] == []


def test_restart_ahead_and_empty_cursors():
    sampler, clock, replay = setup()
    assert replay.view("core")["latest_id"] == 0
    assert replay.view("core")["next_after"] == 0
    with pytest.raises(CursorError, match="instance_changed"):
        replay.view("core", "old-instance")
    with pytest.raises(CursorError, match="cursor_ahead"):
        replay.view("core", sampler.instance_id, 1)
    with pytest.raises(KeyError):
        replay.view("battery")


def test_retention_expires_even_without_new_publications():
    sampler, clock, replay = setup(retention=2)
    sampler._attempt()
    clock.ns = S
    sampler._attempt()
    clock.ns = 3 * S
    page = replay.view("core")
    assert [r["id"] for r in page["records"]] == [2]
    assert page["missing_ranges"] == [{"from": 1, "to": 1, "reason": "buffer_unavailable"}]
    clock.ns += 1
    page = replay.view("core")
    assert page["records"] == [] and page["oldest_id"] is None
    assert page["next_after"] == 2 and page["missing_ranges"][0]["to"] == 2


def test_shared_byte_and_record_caps(monkeypatch):
    sampler, clock, replay = setup(("wifi",), max_bytes=1500)
    for _ in range(6):
        sampler._attempt()
    assert replay._bytes <= replay.max_bytes
    assert replay.view("core")["missing_ranges"]
    monkeypatch.setattr(replay_module, "MAX_RECORDS", 2)
    replay.append("wifi", 1, 0, 0, observation())
    sampler._attempt()
    assert len(replay._records) <= 2
    assert replay.view("wifi")["records"][0]["id"] == 1


def test_oversized_middle_and_tail_are_reported_and_cursor_progresses(monkeypatch):
    sampler, clock, replay = setup()
    sampler._attempt()
    monkeypatch.setattr(replay_module, "MAX_RECORD_BYTES", 100)
    sampler._attempt()  # rejected id 2
    monkeypatch.setattr(replay_module, "MAX_RECORD_BYTES", 1024 * 1024)
    sampler._attempt()
    monkeypatch.setattr(replay_module, "MAX_RECORD_BYTES", 100)
    sampler._attempt()  # rejected tail id 4
    first = replay.view("core", limit=1)
    assert first["next_after"] == 1 and first["has_more"]
    last = replay.view("core", sampler.instance_id, 1)
    assert [r["id"] for r in last["records"]] == [3]
    assert last["next_after"] == 4 and not last["has_more"]
    assert [(g["from"], g["to"]) for g in last["missing_ranges"]] == [(2, 2), (4, 4)]


def test_response_byte_budget_paginates_before_count_limit(monkeypatch):
    sampler, clock, replay = setup()
    for _ in range(4):
        sampler._attempt()
    monkeypatch.setattr(replay_module, "MAX_RESPONSE_BYTES", 6000)
    first = replay.view("core")
    assert len(first["records"]) == 1 and first["has_more"]
    assert len(json.dumps(first, separators=(",", ":")).encode()) < 6000


def observation():
    return {"state": "ok", "stale_after_ms": 30000, "data": {"all": [0, None]},
            "observation": {"age_ms": 0, "data_age_ms": 0, "stale": False}, "collecting_for_ms": None}


@pytest.mark.parametrize("name", ["storage", "wifi", "battery", "probe"])
def test_independent_observation_ids_complete_payloads_and_ages(name):
    sampler, clock, replay = setup((name,))
    data = {"arbitrary": [{"zero": 0, "unknown": None}], "extra": "kept"}
    slow = Slow(name, lambda group: group.got(data), 5, clock)
    slow.replay = replay
    slow.observe_once()
    sampler._attempt()
    slow.observe_once()
    data["extra"] = "mutated"
    page = replay.view(name)
    assert [r["id"] for r in page["records"]] == [1, 2]
    assert all(r["data"]["data"]["extra"] == "kept" for r in page["records"])
    clock.ns = 16 * S
    page = replay.view(name)
    assert page["records"][0]["data"]["state"] == "stale"
    assert page["records"][0]["data"]["observation"]["data_age_ms"] == 16000
    page["records"][0]["data"]["data"]["extra"] = "caller mutation"
    assert replay.view(name)["records"][0]["data"]["data"]["extra"] == "kept"


def test_failed_core_attempt_marks_recovery_without_inventing_sample():
    sampler, clock, replay = setup()
    sampler._attempt()
    sampler.collect = lambda: 1 / 0
    sampler._attempt()
    assert replay.view("core")["latest_id"] == 1
    sampler.collect = lambda: {"value": 2}
    sampler._attempt()
    assert [r["gap_before"] for r in replay.view("core")["records"]] == [False, True]


def test_serialization_failure_records_loss_and_does_not_stop_live_or_history():
    sampler, clock, replay = setup()
    seen = []
    sampler.on_publish = seen.append
    sampler.collect = lambda: {"value": float("nan")}
    sampler._attempt()
    assert len(seen) == 1 and sampler.read()[0] is not None
    page = replay.view("core")
    assert page["latest_id"] == 1 and page["missing_ranges"][0]["to"] == 1


def test_observation_failure_is_archived_with_diagnostics():
    sampler, clock, replay = setup(("battery",))
    slow = Slow("battery", lambda group: 1 / 0, 60, clock)
    slow.replay = replay
    slow.observe_once()
    entry = replay.view("battery")["records"][0]["data"]
    assert entry["data"] is None and entry["collector"]["state"] == "error"


def test_reader_and_writer_do_not_share_mutable_payloads():
    sampler, clock, replay = setup()
    sampler._attempt()
    errors = []

    def reader():
        try:
            for _ in range(100):
                page = replay.view("core", limit=3)
                ids = [r["id"] for r in page["records"]]
                assert ids == sorted(set(ids))
                page["records"][0]["data"]["nested"]["value"] = "changed"
        except Exception as e:
            errors.append(e)
    thread = threading.Thread(target=reader)
    thread.start()
    for _ in range(40):
        sampler._attempt()
    thread.join(5)
    assert not thread.is_alive() and not errors
    assert replay.view("core")["records"][0]["data"]["nested"]["value"] is None
