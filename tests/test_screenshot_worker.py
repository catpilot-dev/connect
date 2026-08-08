"""Tests for the background HUD-screenshot extraction worker."""
import pytest

import screenshot_worker as sw


REAL_EPOCH = 1_780_000_000.0  # well past MIN_REAL_EPOCH


class StubStore:
    def __init__(self):
        self._routes = {}
        self._hidden = set()
        self._metadata = {}

    def add_route(self, lid, create_time=REAL_EPOCH, segs=(0,)):
        self._routes[f"dongle/{lid}"] = {
            "_local_id": lid,
            "create_time": create_time,
            "dongle_id": "dongle",
            "fullname": f"dongle/{lid}",
            "_segments": [{"number": n, "path": f"/data/{lid}--{n}"} for n in segs],
        }
        self._metadata[lid] = {"route_id": lid}


@pytest.fixture
def store():
    return StubStore()


@pytest.fixture
def quiet_world(monkeypatch):
    """Offroad, no existing captures, every segment has a qlog."""
    monkeypatch.setattr(sw, "is_onroad", lambda: False)
    monkeypatch.setattr(sw, "_existing_capture_epochs", lambda: [])
    monkeypatch.setattr(sw.RouteStore, "_find_qlog", staticmethod(lambda p: p + "/qlog.zst"))


def test_collect_creates_pending_job(store, quiet_world, monkeypatch):
    store.add_route("aaaa--11")
    monkeypatch.setattr(sw, "_extract_bookmark_epochs", lambda path, seg: [(12_345, None)])

    jobs, changed = sw._collect_pending(store)

    assert changed
    assert len(jobs) == 1
    job = jobs[0]
    assert job["lid"] == "aaaa--11"
    assert job["offset_ms"] == 12_345
    assert job["epoch"] == pytest.approx(REAL_EPOCH + 12.345)
    state = store._metadata["aaaa--11"]["hud_capture_state"]
    assert state["scanned_segs"] == [0]
    assert state["bookmarks"]["12345"]["status"] == "pending"


def test_route_without_real_epoch_is_skipped(store, quiet_world, monkeypatch):
    store.add_route("bbbb--22", create_time=1234)  # counter fallback, not a clock
    monkeypatch.setattr(sw, "_extract_bookmark_epochs", lambda path, seg: [(5_000, None)])

    jobs, changed = sw._collect_pending(store)

    assert jobs == []
    assert "hud_capture_state" not in store._metadata["bbbb--22"]


def test_existing_capture_marks_done_without_job(store, quiet_world, monkeypatch):
    store.add_route("cccc--33")
    monkeypatch.setattr(sw, "_extract_bookmark_epochs", lambda path, seg: [(10_000, None)])
    monkeypatch.setattr(sw, "_existing_capture_epochs",
                        lambda: [REAL_EPOCH + 10.8])  # within 2s tolerance

    jobs, changed = sw._collect_pending(store)

    assert jobs == []
    assert changed
    state = store._metadata["cccc--33"]["hud_capture_state"]
    assert state["bookmarks"]["10000"]["status"] == "done"


def test_scanned_segments_are_not_rescanned(store, quiet_world, monkeypatch):
    store.add_route("dddd--44")
    calls = []

    def fake_extract(path, seg):
        calls.append(seg)
        return []

    monkeypatch.setattr(sw, "_extract_bookmark_epochs", fake_extract)
    sw._collect_pending(store)
    sw._collect_pending(store)

    assert calls == [0]


def test_exhausted_attempts_marks_failed(store, quiet_world, monkeypatch):
    store.add_route("eeee--55")
    store._metadata["eeee--55"]["hud_capture_state"] = {
        "scanned_segs": [0],
        "bookmarks": {"7000": {"status": "pending", "attempts": sw.MAX_ATTEMPTS}},
    }

    jobs, changed = sw._collect_pending(store)

    assert jobs == []
    state = store._metadata["eeee--55"]["hud_capture_state"]
    assert state["bookmarks"]["7000"]["status"] == "failed"


def test_onroad_aborts_scan(store, quiet_world, monkeypatch):
    store.add_route("ffff--66")
    monkeypatch.setattr(sw, "is_onroad", lambda: True)
    monkeypatch.setattr(sw, "_extract_bookmark_epochs",
                        lambda path, seg: pytest.fail("must not parse qlogs onroad"))

    jobs, changed = sw._collect_pending(store)

    assert jobs == []
    assert not changed


def test_exact_tap_epoch_preferred_over_create_time(store, quiet_world, monkeypatch):
    """PNG naming must use the tap's true wall-time, not create_time + offset."""
    store.add_route("hhhh--88")
    tap_epoch = REAL_EPOCH - 42.0  # GPS fix lagged the tap-relative estimate
    monkeypatch.setattr(sw, "_extract_bookmark_epochs",
                        lambda path, seg: [(20_000, tap_epoch)])

    jobs, changed = sw._collect_pending(store)

    assert len(jobs) == 1
    assert jobs[0]["epoch"] == pytest.approx(tap_epoch)
    state = store._metadata["hhhh--88"]["hud_capture_state"]
    assert state["bookmarks"]["20000"]["epoch"] == pytest.approx(tap_epoch)


def test_epoch_synced_to_visible_bookmarks(store, quiet_world, monkeypatch):
    """Imported drive bookmarks gain the exact epoch for frontend matching."""
    store.add_route("iiii--99")
    store._metadata["iiii--99"]["bookmarks"] = [
        {"time_sec": 20.0, "label": "Drive bookmark"},
        {"time_sec": 300.0, "label": "manual, untouched"},
    ]
    tap_epoch = REAL_EPOCH + 17.5
    monkeypatch.setattr(sw, "_extract_bookmark_epochs",
                        lambda path, seg: [(20_000, tap_epoch)])

    sw._collect_pending(store)

    bms = store._metadata["iiii--99"]["bookmarks"]
    assert bms[0]["epoch"] == pytest.approx(tap_epoch)
    assert "epoch" not in bms[1]


def test_only_latest_route_is_scanned(store, quiet_world, monkeypatch):
    """Captures belong to the current drive — no retroactive backfill."""
    store.add_route("old1--aa", create_time=REAL_EPOCH - 7200)
    store.add_route("new1--bb", create_time=REAL_EPOCH)
    scanned_paths = []

    def fake_extract(path, seg):
        scanned_paths.append(path)
        return []

    monkeypatch.setattr(sw, "_extract_bookmark_epochs", fake_extract)
    sw._collect_pending(store)

    assert scanned_paths == ["/data/new1--bb--0/qlog.zst"]
    assert "hud_capture_state" not in store._metadata["old1--aa"]


def test_older_route_pending_still_renders(store, quiet_world, monkeypatch):
    """Latest-only scanning must not orphan already-discovered taps."""
    store.add_route("old2--cc", create_time=REAL_EPOCH - 7200)
    store.add_route("new2--dd", create_time=REAL_EPOCH)
    store._metadata["old2--cc"]["hud_capture_state"] = {
        "scanned_segs": [0],
        "bookmarks": {"9000": {"status": "pending", "attempts": 1}},
    }
    monkeypatch.setattr(sw, "_extract_bookmark_epochs", lambda path, seg: [])

    jobs, changed = sw._collect_pending(store)

    assert [j["lid"] for j in jobs] == ["old2--cc"]


def test_work_signature_tracks_newest_route(store):
    assert sw._work_signature(store) is None
    store.add_route("old3--ee", create_time=REAL_EPOCH - 100)
    store.add_route("new3--ff", create_time=REAL_EPOCH, segs=(0, 1))
    sig = sw._work_signature(store)
    assert sig == (REAL_EPOCH, "new3--ff", 2)
    # A new drive changes the signature → worker resumes from idle
    store.add_route("new4--gg", create_time=REAL_EPOCH + 500)
    assert sw._work_signature(store) != sig


def test_capture_filename_uses_drive_timezone():
    # 2026-08-06 06:40:28 UTC == 14:40:28 at UTC+8, 01:40:28 at UTC-5
    epoch = 1785998428.0
    assert sw._capture_filename(epoch, 8) == "capture_20260806_144028.png"
    assert sw._capture_filename(epoch, -5) == "capture_20260806_014028.png"


def test_state_epoch_map_is_matching_source_of_truth():
    """Local-time names don't encode the epoch — the state map must."""
    from handlers.screenshots import _state_epoch_map
    store = StubStore()
    store.add_route("jjjj--00")
    store._metadata["jjjj--00"]["hud_capture_state"] = {
        "scanned_segs": [0],
        "bookmarks": {
            "5000": {"status": "done", "epoch": 1785998428.4,
                     "file": "capture_20260806_144028.png"},
            "9000": {"status": "pending", "attempts": 0},  # no file yet
        },
    }
    assert _state_epoch_map(store) == {
        "capture_20260806_144028.png": pytest.approx(1785998428.4),
    }
    assert _state_epoch_map(None) == {}


def test_parse_capture_epoch_plain_name_still_works():
    from handlers.screenshots import _parse_capture_epoch
    # Plugin-style offroad name, device-local — must parse, not crash
    assert _parse_capture_epoch("capture_20260315_035151.png") is not None
    assert _parse_capture_epoch("not_a_capture.png") is None


def test_record_result_success_and_failure(store):
    store.add_route("gggg--77")
    store._metadata["gggg--77"]["hud_capture_state"] = {
        "scanned_segs": [0],
        "bookmarks": {"3000": {"status": "pending", "attempts": 0}},
    }
    job = {"lid": "gggg--77", "offset_ms": 3000}
    entry = store._metadata["gggg--77"]["hud_capture_state"]["bookmarks"]["3000"]

    sw._record_result(store, job, ok=False, err="boom")
    assert entry["attempts"] == 1
    assert entry["status"] == "pending"
    assert entry["error"] == "boom"

    sw._record_result(store, job, ok=True, err=None)
    assert entry["status"] == "done"
    assert "error" not in entry
