"""ros.request, follow_route.through and project files."""

import asyncio
import json

from conftest import example
from mission_runner.types import RunStatus


def mission(name: str, flow: list, **extra) -> dict:
    return {"schema": "mission/1", "name": name, "flow": flow, **extra}


async def _published_request(harness, topic: str, count: int = 1, timeout: float = 5.0) -> dict:
    b = harness.r.backend
    t0 = asyncio.get_running_loop().time()
    while asyncio.get_running_loop().time() - t0 < timeout:
        reqs = [m for t, _ty, m in b.published if t == topic]
        if len(reqs) >= count:
            return json.loads(reqs[count - 1]["data"])
        await asyncio.sleep(0.02)
    raise AssertionError(f"no request published on {topic}")


async def test_ros_request_answer_and_timeout_default(harness):
    doc = mission(
        "ask",
        [
            {"id": "go", "type": "nav.follow_route", "to": "A"},
            {"id": "q", "type": "ros.request", "text": "Part OK?", "options": ["OK", "Reject"], "timeout_s": 10, "data": {"n": "${1 + 1}"}, "out": "ans"},
            {"id": "chk", "type": "if", "condition": "ans.value.answer == 'Reject'", "then": [{"id": "e", "type": "end", "result": "failed", "message": "rejected"}]},
            {"id": "q2", "type": "ros.request", "text": "Anybody?", "options": ["Yes", "No"], "default": "No", "timeout_s": 0.3, "out": "ans2"},
            {"id": "log", "type": "log", "text": "second ${ans2.value.answer} by ${ans2.value.by}"},
        ],
    )
    await harness.r.deploy_mission(doc)
    run = await harness.run("ask")
    body = await _published_request(harness, "/iviz/request")
    assert body["text"] == "Part OK?" and body["options"] == ["OK", "Reject"]
    assert body["station"] == "A" and body["data"] == {"n": 2}
    assert body["mission"] == "ask" and body["step_id"] == "q" and len(body["id"]) == 8
    # an answer for another request is ignored
    harness.r.sim_topics.push("/iviz/answer", {"data": json.dumps({"id": "someone-else", "answer": "Reject"})})
    await asyncio.sleep(0.1)
    assert run.status == RunStatus.RUNNING
    harness.r.sim_topics.push("/iviz/answer", {"data": json.dumps({"id": body["id"], "answer": "OK", "by": "test"})})
    assert await harness.wait_idle(30)
    assert run.status == RunStatus.SUCCEEDED, run.error
    assert "second No by timeout" in harness.logs(run.id)
    answered = [e for e in harness.events if e["type"] == "request.answered"]
    assert answered and answered[0]["answer"] == "OK" and answered[0]["by"] == "test"


async def test_ros_request_rejected_answer_branches(harness):
    doc = mission(
        "ask_reject",
        [
            {"id": "q", "type": "ros.request", "text": "Part OK?", "options": ["OK", "Reject"], "timeout_s": 10, "out": "ans"},
            {"id": "chk", "type": "if", "condition": "ans.value.answer == 'Reject'", "then": [{"id": "e", "type": "end", "result": "failed", "message": "rejected"}]},
        ],
    )
    await harness.r.deploy_mission(doc)
    run = await harness.run("ask_reject")
    body = await _published_request(harness, "/iviz/request")
    harness.r.sim_topics.push("/iviz/answer", {"data": json.dumps({"id": body["id"], "answer": "Reject"})})
    assert await harness.wait_idle(10)
    assert run.status == RunStatus.FAILED and run.error == "rejected"


async def test_ros_request_custom_topics_and_timeout_fail(harness):
    doc = mission("ask_fail", [{"id": "q", "type": "ros.request", "text": "?", "timeout_s": 0.3, "on_timeout": "fail", "request_topic": "/cell/req", "answer_topic": "/cell/ans"}])
    await harness.r.deploy_mission(doc)
    run = await harness.run("ask_fail")
    body = await _published_request(harness, "/cell/req")
    assert "station" not in body
    assert await harness.wait_idle(10)
    assert run.status == RunStatus.FAILED and "no answer on /cell/ans" in run.error


async def test_ros_request_uses_the_station_sites_topics(harness):
    m = harness.r.store.sites.map("demo_room")
    m.sites["A"].request_topic, m.sites["A"].answer_topic = "/station/a/request", "/station/a/answer"
    m.sites["B"].request_topic = "/station/b/request"  # answer falls back to the default
    doc = mission(
        "ask_stations",
        [
            {"id": "go", "type": "nav.follow_route", "to": "A"},
            {"id": "qa", "type": "ros.request", "text": "At A?", "timeout_s": 10},
            {"id": "qb", "type": "ros.request", "text": "At B?", "station": "B", "timeout_s": 10},
            {"id": "qo", "type": "ros.request", "text": "Override", "station": "A", "request_topic": "/own/req", "timeout_s": 10},
        ],
    )
    await harness.r.deploy_mission(doc)
    run = await harness.run("ask_stations")
    body = await _published_request(harness, "/station/a/request")
    assert body["station"] == "A"
    harness.r.sim_topics.push("/iviz/answer", {"data": json.dumps({"id": body["id"], "answer": "OK"})})  # wrong topic: ignored
    await asyncio.sleep(0.1)
    assert run.status == RunStatus.RUNNING
    harness.r.sim_topics.push("/station/a/answer", {"data": json.dumps({"id": body["id"], "answer": "OK"})})
    body = await _published_request(harness, "/station/b/request")
    assert body["station"] == "B"
    harness.r.sim_topics.push("/iviz/answer", {"data": json.dumps({"id": body["id"], "answer": "OK"})})
    body = await _published_request(harness, "/own/req")
    harness.r.sim_topics.push("/station/a/answer", {"data": json.dumps({"id": body["id"], "answer": "OK"})})
    assert await harness.wait_idle(30)
    assert run.status == RunStatus.SUCCEEDED, run.error
    assert m.sites["B"].as_dict()["request_topic"] == "/station/b/request" and "answer_topic" not in m.sites["B"].as_dict()


async def test_follow_route_through(harness):
    doc = mission("thru", [{"id": "go", "type": "nav.follow_route", "through": ["C"], "to": "Rack3"}])
    await harness.r.deploy_mission(doc)
    run = await harness.run("thru")
    assert await harness.wait_idle(120)
    assert run.status == RunStatus.SUCCEEDED, run.error
    value = [e["result"]["value"] for e in harness.events if e["type"] == "step.finished" and e["step_id"] == "go"][0]
    assert value["through"] == ["C"]
    assert value["route"] == ["Home", "D", "C", "B", "Conveyor1", "Rack3"]


async def test_follow_route_from_one_way_dead_end_does_not_leave_the_lanes(harness):
    # Robot parked at Charger, reachable only by a one-way lane Home -> Charger:
    # the route must start at Charger and fail, not start at Home and cut across.
    m = harness.r.store.sites.map("demo_room")
    for e in m.edges:
        if {e.frm, e.to} == {"Home", "Charger"}:
            e.frm, e.to, e.bidirectional = "Home", "Charger", False
    c = m.sites["Charger"]
    state = harness.r.backend._state
    state.x, state.y = c.x, c.y
    doc = mission("deadend", [{"id": "go", "type": "nav.follow_route", "to": "A"}])
    await harness.r.deploy_mission(doc)
    run = await harness.run("deadend")
    assert await harness.wait_idle(30)
    assert run.status == RunStatus.FAILED
    assert "no route from 'Charger' to 'A'" in str(run.error)


async def test_project_export_and_import(harness):
    status, proj = await harness.r.call_api("GET", "/api/project?name=demo")
    assert status == 200 and proj["schema"] == "project/1" and proj["name"] == "demo"
    assert {"patrol", "route_delivery"} <= {m["name"] for m in proj["missions"]}
    assert proj["settings"] == {"request_topic": "/iviz/request", "answer_topic": "/iviz/answer"}

    proj["missions"] = [m for m in proj["missions"] if m["name"] != "patrol"] + [mission("new_one", [{"type": "log", "text": "hi"}])]
    proj["sites"]["maps"]["demo_room"]["sites"]["Dock2"] = {"x": 2.0, "y": 2.0}
    status, out = await harness.r.call_api("PUT", "/api/project?replace=true", proj)
    assert status == 200 and out["ok"], out
    assert "patrol" in out["deleted"] and "new_one" in out["saved"]
    assert "patrol" not in harness.r.store.names() and "new_one" in harness.r.store.names()
    assert harness.r.store.sites.lookup("demo_room", "Dock2") is not None

    bad = {**proj, "missions": [*proj["missions"], mission("broken", [{"type": "nav.follow_route", "to": "Nowhere"}])]}
    before = set(harness.r.store.names())
    status, out = await harness.r.call_api("PUT", "/api/project", bad)
    assert status == 400 and not out["ok"]
    assert any("Nowhere" in e["message"] and e["path"].startswith("missions/broken") for e in out["errors"])
    assert set(harness.r.store.names()) == before


def test_cli_project_import_export(tmp_path):
    from mission_runner.cli import main

    home = tmp_path / "home"
    proj = {"schema": "project/1", "name": "t", "sites": example("sites"), "missions": [example("go_to_point"), example("route_delivery")]}
    f = tmp_path / "p.mproj"
    f.write_text(json.dumps(proj), encoding="utf-8")
    assert main(["project", "import", str(f), "--home", str(home)]) == 0
    assert (home / "missions" / "route_delivery.json").exists() and (home / "sites.json").exists()

    out = tmp_path / "out.mproj"
    assert main(["project", "export", str(out), "--home", str(home), "--name", "line3"]) == 0
    back = json.loads(out.read_text("utf-8"))
    assert back["name"] == "line3" and {m["name"] for m in back["missions"]} == {"go_to_point", "route_delivery"}

    bad = {**proj, "missions": [mission("x", [{"type": "nav.follow_route", "to": "Nowhere"}])]}
    f.write_text(json.dumps(bad), encoding="utf-8")
    h2 = tmp_path / "h2"
    assert main(["project", "import", str(f), "--home", str(h2)]) == 1
    assert not (h2 / "missions" / "x.json").exists()


def test_cli_run_with_project_imports_before_starting(tmp_path, monkeypatch, capsys):
    from mission_runner import cli

    started: list[set] = []

    async def fake_serve(cfg):
        started.append({p.stem for p in (cfg.home / "missions").glob("*.json")})
        return 0

    monkeypatch.setattr(cli, "_serve", fake_serve)
    home = tmp_path / "home"
    proj = {"schema": "project/1", "name": "t", "sites": example("sites"), "missions": [example("go_to_point")]}
    f = tmp_path / "p.mproj"
    f.write_text(json.dumps(proj), encoding="utf-8")
    assert cli.main(["run", "--sim", "--home", str(home), "--project", str(f), "--ros-args", "-r", "__node:=x"]) == 0
    assert started == [{"go_to_point"}]

    # --project-replace removes what is not in the project; an empty --project (the launch default) is ignored
    (home / "missions" / "old.json").write_text(json.dumps(mission("old", [{"type": "log", "text": "x"}])), encoding="utf-8")
    assert cli.main(["run", "--sim", "--home", str(home), "--project", str(f), "--project-replace"]) == 0
    assert started[-1] == {"go_to_point"}
    assert cli.main(["run", "--sim", "--home", str(home), "--project", ""]) == 0 and len(started) == 3

    bad = {**proj, "missions": [mission("x", [{"type": "nav.follow_route", "to": "Nowhere"}])]}
    f.write_text(json.dumps(bad), encoding="utf-8")
    h2 = tmp_path / "h2"
    assert cli.main(["run", "--sim", "--home", str(h2), "--project", str(f)]) == 1
    assert len(started) == 3 and not (h2 / "missions" / "x.json").exists()
    assert "not started" in capsys.readouterr().out
