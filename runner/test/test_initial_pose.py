"""Home as initial pose: sites/1 initial_pose, the confirm loop, the start-up
task and POST /api/robot/initial_pose (sim backend and fakes, no ROS)."""

import asyncio
import copy
import json

import aiohttp
import pytest

from conftest import _port
from mission_runner.config import RunnerConfig
from mission_runner.localize import confirm_initial_pose
from mission_runner.model import MissionValidationError, SitesBook
from mission_runner.project import check_project, export_project
from mission_runner.runner import Runner
from mission_runner.types import StepTimeout, TaskCanceled


# ----- model ---------------------------------------------------------------------


def test_initial_pose_parse_roundtrip_and_default(sites_doc):
    doc = copy.deepcopy(sites_doc)
    doc["maps"]["demo_room"]["initial_pose"] = {"site": "Home"}
    book = SitesBook.from_dict(doc)
    ip = book.map("demo_room").initial_pose
    assert ip is not None and ip.site == "Home" and ip.on_start is True
    assert book.map("warehouse_b").initial_pose is None
    out = book.to_dict()
    assert out["maps"]["demo_room"]["initial_pose"] == {"site": "Home", "on_start": True}
    assert "initial_pose" not in out["maps"]["warehouse_b"]
    assert SitesBook.from_dict(out).to_dict() == out

    doc["maps"]["demo_room"]["initial_pose"] = {"site": "C", "on_start": False}
    assert SitesBook.from_dict(doc).to_dict()["maps"]["demo_room"]["initial_pose"] == {"site": "C", "on_start": False}


def test_initial_pose_validation(sites_doc):
    doc = copy.deepcopy(sites_doc)
    doc["maps"]["warehouse_b"]["initial_pose"] = {"site": "Rack3"}  # only in demo_room
    with pytest.raises(MissionValidationError) as ei:
        SitesBook.from_dict(doc)
    assert ei.value.errors[0].path == ["maps", "warehouse_b", "initial_pose", "site"]
    assert "does not exist in map 'warehouse_b'" in ei.value.errors[0].message

    for bad in ({"on_start": True}, {"site": "Home", "on_start": "yes"}, {"site": "Home", "extra": 1}, {"site": ""}, "Home"):
        doc = copy.deepcopy(sites_doc)
        doc["maps"]["demo_room"]["initial_pose"] = bad
        with pytest.raises(MissionValidationError):
            SitesBook.from_dict(doc)


async def test_initial_pose_project_export_import(harness, sites_doc):
    doc = copy.deepcopy(sites_doc)
    doc["maps"]["demo_room"]["initial_pose"] = {"site": "Home", "on_start": False}
    harness.r.store.save_sites(doc)
    project = export_project(harness.r.store, "p")
    assert project["sites"]["maps"]["demo_room"]["initial_pose"] == {"site": "Home", "on_start": False}
    errors, _w, book, _m = check_project(json.loads(json.dumps(project)))
    assert not errors and book.map("demo_room").initial_pose.on_start is False
    project["sites"]["maps"]["demo_room"]["initial_pose"]["site"] = "Nowhere"
    errors, _w, book, _m = check_project(project)
    assert book is None and errors[0].path == ["sites", "maps", "demo_room", "initial_pose", "site"]


# ----- confirm loop (fake clock) ----------------------------------------------------


class FakeClock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def test_confirm_loop_republishes_until_confirmed():
    clock = FakeClock()
    sent = []
    # the localizer answers 2.5 s after the first message
    n = confirm_initial_pose(lambda: sent.append(clock.t), lambda t_first: clock.t - t_first >= 2.5, clock=clock, sleep=clock.sleep)
    assert n == len(sent) == 3
    assert [round(t - sent[0], 1) for t in sent] == [0.0, 1.0, 2.0]


def test_confirm_loop_times_out_and_stops():
    clock = FakeClock()
    sent = []
    with pytest.raises(StepTimeout) as ei:
        confirm_initial_pose(lambda: sent.append(1), lambda _t: False, timeout_s=30.0, clock=clock, sleep=clock.sleep)
    assert 29 <= len(sent) <= 31 and "within 30 s" in str(ei.value)

    clock = FakeClock()
    with pytest.raises(TaskCanceled):
        confirm_initial_pose(lambda: None, lambda _t: False, should_stop=lambda: clock.t > 103, clock=clock, sleep=clock.sleep)


def test_confirm_loop_immediate():
    clock = FakeClock()
    assert confirm_initial_pose(lambda: None, lambda _t: True, clock=clock, sleep=clock.sleep) == 1


# ----- HTTP API ----------------------------------------------------------------------


async def test_api_initial_pose_by_site_and_coordinates(harness):
    base = f"http://127.0.0.1:{harness.r.config.http_port}/api/robot/initial_pose"
    async with aiohttp.ClientSession() as s:
        async with s.post(base, json={"site": "Rack3"}) as resp:
            assert resp.status == 200
            assert await resp.json() == {"ok": True, "site": "Rack3", "x": 3.0, "y": -3.0, "yaw_deg": -90.0}
        st = harness.r.backend.robot_state()
        assert (st.x, st.y, st.yaw_deg) == (3.0, -3.0, -90.0)
        async with s.post(base, json={"x": 1.5, "y": -0.5, "yaw_deg": 45}) as resp:
            assert resp.status == 200
            assert await resp.json() == {"ok": True, "x": 1.5, "y": -0.5, "yaw_deg": 45.0}
        async with s.post(base, json={"site": "Nowhere"}) as resp:
            assert resp.status == 400 and "Nowhere" in (await resp.json())["error"]
        for bad in ({}, {"x": 1}, {"x": "1", "y": 2}, {"x": True, "y": 2}, [1, 2], {"site": 3}):
            async with s.post(base, json=bad) as resp:
                assert resp.status == 400, bad
        # the ROS /mission/api route goes through the same handler
        status, body = await harness.r.call_api("POST", "/api/robot/initial_pose", {"site": "Home"})
        assert status == 200 and body["site"] == "Home"
    ev = [e for e in harness.events if e["type"] == "robot.initial_pose"]
    assert [(e["site"], e["source"], e["ok"]) for e in ev] == [("Rack3", "api", True), (None, "api", True), ("Home", "api", True)]
    assert ev[1]["x"] == 1.5 and ev[1]["yaw_deg"] == 45.0


async def test_api_initial_pose_refused_while_moving(harness):
    run = await harness.run("patrol", {"rounds": 1})
    await harness.wait_step(run.id, "lap")
    base = f"http://127.0.0.1:{harness.r.config.http_port}/api/robot/initial_pose"
    async with aiohttp.ClientSession() as s:
        async with s.post(base, json={"site": "Home"}) as resp:
            assert resp.status == 409 and "stop the mission first" in (await resp.json())["message"]
    await harness.r.dispatcher.stop_all("test")
    assert await harness.wait_idle()


async def test_step_emits_initial_pose_event(harness):
    doc = {"schema": "mission/1", "name": "ip", "flow": [{"id": "s", "type": "nav.set_initial_pose", "pose": "C"}]}
    await harness.r.deploy_mission(doc)
    run = await harness.run("ip")
    assert await harness.wait_idle()
    assert run.status.value == "succeeded", run.error
    ev = [e for e in harness.events if e["type"] == "robot.initial_pose"]
    assert ev and ev[-1]["source"] == "step" and ev[-1]["site"] == "C" and ev[-1]["ok"]


# ----- start-up task -----------------------------------------------------------------


async def _start_runner(tmp_path, sites_doc, initial_pose, localized=False):
    doc = copy.deepcopy(sites_doc)
    if initial_pose is not None:
        doc["maps"]["demo_room"]["initial_pose"] = initial_pose
    (tmp_path / "sites.json").write_text(json.dumps(doc), "utf-8")
    _port[0] += 1
    cfg = RunnerConfig.load(tmp_path, backend="sim", http_port=_port[0], http_host="127.0.0.1")
    runner = Runner(cfg)
    runner.backend.localized = localized
    events: list[dict] = []
    runner.events.subscribe(events.append)
    await runner.start()
    for _ in range(50):
        if not any(t.get_name() == "initial-pose" for t in runner._tasks):
            break
        await asyncio.sleep(0.02)
    return runner, events


@pytest.mark.parametrize(
    ("initial_pose", "localized", "moved", "log_part"),
    [
        ({"site": "C"}, False, True, "initial pose set at C (-1.00, 0.00, 90 deg)"),
        ({"site": "C", "on_start": False}, False, False, "on_start is off"),
        ({"site": "C"}, True, False, "robot already localized, not touching it"),
        (None, False, False, None),
    ],
)
async def test_start_sets_home_pose(tmp_path, sites_doc, initial_pose, localized, moved, log_part):
    runner, events = await _start_runner(tmp_path, sites_doc, initial_pose, localized)
    try:
        st = runner.backend.robot_state()
        ev = [e for e in events if e["type"] == "robot.initial_pose"]
        if moved:
            assert (st.x, st.y, st.yaw_deg) == (-1.0, 0.0, 90.0)
            assert ev == [{**ev[0], "site": "C", "x": -1.0, "y": 0.0, "yaw_deg": 90.0, "source": "start", "ok": True}]
        else:
            assert (st.x, st.y) == (0.0, 0.0) and not ev
        logs = [e["text"] for e in events if e["type"] == "log" and "initial pose" in e["text"]]
        if log_part:
            assert any(log_part in t for t in logs), logs
        else:
            assert not logs
    finally:
        await runner.stop()
