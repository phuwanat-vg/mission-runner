"""End-to-end tests against the sim backend (no ROS, no network)."""

import asyncio
import json

import pytest

from mission_runner.model import MissionValidationError
from mission_runner.types import RunSource, RunStatus

pytestmark = pytest.mark.asyncio


def mission(name: str, flow: list, **extra) -> dict:
    return {"schema": "mission/1", "name": name, "flow": flow, **extra}


async def test_examples_installed_and_armed(harness):
    r = harness.r
    assert {"go_to_point", "patrol", "pickup_job", "global"} <= r.store.names()
    armed = r.triggers.armed_summary()
    assert armed["patrol"] == ["cron 0 22 * * *"]
    assert armed["pickup_job"] == ["POST /hooks/pickup"]  # modbus trigger not armed: no connector
    assert "plc_start" in r.trigger_problems["pickup_job"][0]
    assert r.current_map == "demo_room"
    caps = r.capabilities()
    assert caps["steps"]["nav.dock"]["available"] and caps["triggers"]["timer.cron"]["available"]


async def test_go_to_point(harness):
    run = await harness.run("go_to_point")
    assert await harness.wait_idle()
    assert run.status == RunStatus.SUCCEEDED, run.error
    assert harness.steps(run.id) == [("wait", "succeeded"), ("go", "succeeded"), ("done", "succeeded")]
    robot = harness.r.robot_scope()
    assert abs(robot["x"] - 1.0) < 1e-6 and abs(robot["y"] - 0.5) < 1e-6 and abs(robot["yaw_deg"] + 90) < 1e-6
    assert harness.logs(run.id)[-1].startswith("Goal reached in")
    hist = harness.r.run_log.get_run(run.id)
    assert hist["status"] == "succeeded" and any(e["type"] == "step.finished" for e in hist["events"])


async def test_inputs_vars_loops_and_sites(harness):
    run = await harness.run("patrol", {"rounds": 2})
    assert await harness.wait_idle(60)
    assert run.status == RunStatus.SUCCEEDED, run.error
    assert harness.logs(run.id).count("Lap done") == 2
    assert harness.r.backend.odometer > 10


async def test_retry_before_retry_and_abort(harness):
    b = harness.r.backend
    b.fail_next = True
    run = await harness.run("go_to_point")  # retry 1 with clear_costmap before retry
    assert await harness.wait_idle()
    assert run.status == RunStatus.SUCCEEDED
    steps = harness.steps(run.id)
    assert ("flow_1_on_fail_before_retry_0", "succeeded") in steps
    assert any(e["type"] == "step.retry" for e in harness.events)

    from mission_runner.backends.base import Pose

    b.fail_at = [Pose(1.0, 0.5)]
    run = await harness.run("go_to_point")
    assert await harness.wait_idle()
    assert run.status == RunStatus.FAILED and "simulated failure" in run.error
    b.fail_at = []


async def test_on_fail_continue_and_end(harness):
    doc = mission("cont", [
        {"id": "bad", "type": "nav.go_to_pose", "pose": {"x": 99, "y": 99}, "on_fail": {"then": "continue"}, "out": "bad"},
        {"id": "chk", "type": "if", "condition": "not bad.ok", "then": [{"id": "e", "type": "end", "result": "failed", "message": "gave up"}]},
        {"id": "never", "type": "log", "text": "unreachable"},
    ])
    from mission_runner.backends.base import Pose

    harness.r.backend.fail_at = [Pose(99, 99)]
    await harness.r.deploy_mission(doc)
    run = await harness.run("cont")
    assert await harness.wait_idle()
    assert run.status == RunStatus.FAILED and run.error == "gave up"
    assert "unreachable" not in harness.logs(run.id)


async def test_pause_resume_reruns_step(harness):
    run = await harness.run("follow_line")
    await harness.wait_step(run.id, "line")
    await asyncio.sleep(0.15)
    ok, _ = await harness.r.dispatcher.pause()
    assert ok
    await asyncio.sleep(0.2)
    assert run.status == RunStatus.PAUSED and harness.r.dispatcher.state == "paused"
    assert not harness.r.backend._busy
    ok, _ = await harness.r.dispatcher.resume()
    assert ok
    assert await harness.wait_idle(60)
    assert run.status == RunStatus.SUCCEEDED, run.error
    started = [e["step_id"] for e in harness.events if e["type"] == "step.started" and e["run_id"] == run.id]
    assert started.count("line") == 1  # one step.started; the attempt restarted silently
    assert ("run.paused", "follow_line", "paused") in harness.run_events()


async def test_interrupt_and_resume(harness):
    run = await harness.run("patrol", {"rounds": 1})
    await harness.wait_step(run.id, "lap")
    await asyncio.sleep(0.1)
    assert harness.r.triggers.armed_summary()["patrol:interrupts"]
    n = harness.r.sim_topics.push("/battery_state", {"percentage": 0.1})
    assert n == 1
    await asyncio.sleep(0.05)
    d = harness.r.dispatcher
    assert run.status in (RunStatus.SUSPENDED, RunStatus.RUNNING)
    if run.status == RunStatus.SUSPENDED:
        assert d.current is not None and d.current.mission == "go_charge"
        assert [s.mission for s, _ in d.suspended] == ["patrol"]
    assert await harness.wait_idle(90)
    assert [k for k, m, _ in harness.run_events() if m == "go_charge"] == ["run.queued", "run.started", "run.finished"]
    assert run.status == RunStatus.SUCCEEDED, run.error
    kinds = [k for k, m, _ in harness.run_events() if m == "patrol"]
    assert kinds == ["run.queued", "run.started", "run.suspended", "run.resumed", "run.finished"]
    resumed = [e for e in harness.events if e["type"] == "step.started" and e["run_id"] == run.id and e["resumed"]]
    assert resumed and resumed[0]["step_id"] == "lap"
    # rising edge + debounce: a second low reading does not fire again
    assert harness.r.sim_topics.push("/battery_state", {"percentage": 0.1}) == 0  # patrol finished, interrupts disarmed


async def test_policies(harness):
    d = harness.r.dispatcher
    run1 = await harness.run("follow_line")
    await harness.wait_step(run1.id, "line")
    # queue
    run2 = await harness.run("go_to_point")
    assert d.current is run1 and [q.id for q in d.queue] == [run2.id]
    # reject_if_busy
    ok, _, reason = await harness.r.run_mission("go_to_point", {}, RunSource("manual", "t"), policy="reject_if_busy")
    assert not ok and reason == "busy"
    # preempt_latest drops the queued go_to_point and cancels run1
    ok, run3, reason = await harness.r.run_mission("go_to_point", {}, RunSource("manual", "t"), policy="preempt_latest")
    assert ok and reason == "preempting"
    await asyncio.sleep(0.3)
    assert run2.status == RunStatus.CANCELED and "replaced" in run2.error
    assert run1.status == RunStatus.CANCELED and "preempted" in run1.error
    assert await harness.wait_idle()
    assert run3.status == RunStatus.SUCCEEDED
    # lower priority preempt just queues
    run4 = await harness.run("follow_line")
    await harness.wait_step(run4.id, "line")
    ok, run5, reason = await harness.r.run_mission("go_to_point", {}, RunSource("manual", "t"), policy="preempt", priority=10)
    assert ok and "queued" in reason and d.current is run4
    await harness.r.dispatcher.stop_all("test")
    assert run4.status == RunStatus.CANCELED and run5.status == RunStatus.CANCELED


async def test_stop_all_runs_on_abort(harness):
    run = await harness.run("patrol", {"rounds": 3})
    await harness.wait_step(run.id, "lap")
    await harness.r.dispatcher.stop_all("emergency")
    assert run.status == RunStatus.CANCELED and run.error == "emergency"
    assert "Patrol aborted" in harness.logs(run.id)
    assert harness.r.dispatcher.state == "idle"


async def test_wait_event_webhook_ask_user_and_interpolation(harness):
    doc = mission("hook", [
        {"id": "w", "type": "wait_event", "source": {"type": "http.webhook", "path": "loaded"}, "timeout_s": 5, "out": "ev"},
        {"id": "a", "type": "ask_user", "text": "Go on?", "options": ["Yes", "No"], "timeout_s": 5, "default": "Yes", "out": "ans"},
        {"id": "l", "type": "log", "text": "got ${ev.value.x} answer ${ans.value}"},
    ])
    await harness.r.deploy_mission(doc)
    run = await harness.run("hook")
    await harness.wait_step(run.id, "w")
    assert harness.r.internal.push_webhook("loaded", {"x": 42}) == 1
    await harness.wait_step(run.id, "a")
    prompt = harness.r.prompts.current
    assert prompt is not None and prompt.options == ["Yes", "No"]
    assert harness.r.prompts.answer("bogus", "Yes") == (False, "no such prompt")
    assert harness.r.prompts.answer(prompt.id, "Maybe")[0] is False
    assert harness.r.prompts.answer(prompt.id, "No") == (True, "ok")
    assert await harness.wait_idle()
    assert run.status == RunStatus.SUCCEEDED and harness.logs(run.id) == ["got 42 answer No"]


async def test_wait_event_timeout_and_prompt_default(harness):
    doc = mission("tmo", [
        {"id": "w", "type": "wait_event", "source": {"type": "http.webhook", "path": "never"}, "timeout_s": 0.3, "on_timeout": "continue", "out": "ev"},
        {"id": "a", "type": "ask_user", "text": "?", "options": ["A", "B"], "timeout_s": 0.3, "default": "B", "out": "ans"},
        {"id": "w2", "type": "wait_event", "source": {"type": "http.webhook", "path": "never"}, "timeout_s": 0.3},
    ])
    await harness.r.deploy_mission(doc)
    run = await harness.run("tmo")
    assert await harness.wait_idle()
    assert run.status == RunStatus.FAILED and "no event" in run.error
    assert harness.steps(run.id)[:2] == [("w", "succeeded"), ("a", "succeeded")]
    answered = [e for e in harness.events if e["type"] == "prompt.answered"]
    assert answered and answered[0]["by"] == "timeout" and answered[0]["answer"] == "B"


async def test_step_timeout_cancels_navigation(harness):
    doc = mission("slow", [{"id": "g", "type": "nav.go_to_pose", "pose": {"x": 50, "y": 50}, "timeout_s": 0.3}])
    await harness.r.deploy_mission(doc)
    run = await harness.run("slow")
    assert await harness.wait_idle()
    assert run.status == RunStatus.FAILED and "timeout" in run.error
    assert not harness.r.backend._busy


async def test_sub_mission_and_mission_done_trigger(harness):
    parent = mission("parent", [
        {"id": "sub", "type": "run_mission", "mission": "go_to_point", "out": "sub"},
        {"id": "l", "type": "log", "text": "sub ok=${sub.ok}"},
    ])
    chained = mission("after", [{"id": "l", "type": "log", "text": "chained"}], triggers=[{"id": "t", "type": "mission.done", "mission": "parent", "result": "success"}])
    await harness.r.deploy_mission(parent)
    await harness.r.deploy_mission(chained)
    run = await harness.run("parent")
    assert await harness.wait_idle()
    assert run.status == RunStatus.SUCCEEDED and harness.logs(run.id) == ["sub ok=true"]
    assert "chained" in harness.logs()
    kinds = [(k, m) for k, m, _ in harness.run_events()]
    assert ("run.started", "go_to_point") in kinds and ("run.started", "after") in kinds


async def test_change_map_switches_sites(harness):
    doc = mission("maps", [
        {"id": "m", "type": "nav.change_map", "map": "warehouse_b"},
        {"id": "g", "type": "nav.go_to_pose", "pose": "Inbound"},
        {"id": "back", "type": "nav.change_map", "map": "demo_room"},
    ])
    await harness.r.deploy_mission(doc)
    run = await harness.run("maps")
    assert await harness.wait_idle(60)
    assert run.status == RunStatus.SUCCEEDED, run.error
    assert harness.r.backend.current_map_file == "/home/pi/maps/demo_room.yaml"
    assert harness.r.current_map == "demo_room"


async def test_follow_path_and_smoothing_values(harness):
    run = await harness.run("follow_line")
    assert await harness.wait_idle(60)
    assert run.status == RunStatus.SUCCEEDED, run.error
    fin = {e["step_id"]: e["result"] for e in harness.events if e["type"] == "step.finished" and e["run_id"] == run.id}
    assert fin["line"]["value"]["length_m"] > 9
    assert fin["smooth"]["value"]["smoothed"] is True and len(fin["plan"]["value"]["poses"]) > 2


async def test_behaviors_and_generic_ros_steps(harness):
    doc = mission("misc", [
        {"id": "s", "type": "nav.spin", "angle_deg": 90},
        {"id": "b", "type": "nav.backup", "distance_m": 0.5, "speed_mps": 0.5},
        {"id": "d", "type": "nav.drive_on_heading", "distance_m": 0.5, "speed_mps": 0.5},
        {"id": "p", "type": "ros.publish", "topic": "/x", "msg_type": "std_msgs/msg/String", "message": {"data": "hi ${last.ok}"}},
        {"id": "c", "type": "ros.call_service", "service": "/svc", "srv_type": "std_srvs/srv/Trigger", "out": "svc"},
        {"id": "a", "type": "ros.call_action", "action": "/act", "action_type": "nav2_msgs/action/Spin", "goal": {"target_yaw": 1.0}},
        {"id": "prm", "type": "ros.set_param", "node": "/controller_server", "params": {"FollowPath.max_vel_x": 0.2}},
        {"id": "dock", "type": "nav.dock", "dock_id": "Charger"},
        {"id": "undock", "type": "nav.undock"},
        {"id": "cc", "type": "nav.clear_costmap", "which": "local"},
        {"id": "set", "type": "set", "var": "n", "value": "${1 + 2}"},
        {"id": "w", "type": "wait", "seconds": 0.05},
    ])
    await harness.r.deploy_mission(doc)
    run = await harness.run("misc")
    assert await harness.wait_idle(60)
    assert run.status == RunStatus.SUCCEEDED, run.error
    b = harness.r.backend
    assert b.published == [("/x", "std_msgs/msg/String", {"data": "hi true"})]
    assert b.service_calls[0][0] == "/svc" and b.action_calls[0][2] == {"target_yaw": 1.0}
    assert b.params_set == [("/controller_server", {"FollowPath.max_vel_x": 0.2})]
    assert abs((b.robot_state().yaw_deg or 0) - 90) < 1e-6
    assert all(s == "succeeded" for _, s in harness.steps(run.id))


async def test_deploy_validation_and_delete(harness):
    bad = mission("bad", [{"type": "nav.go_to_pose", "pose": "Nowhere"}])
    with pytest.raises(MissionValidationError):
        await harness.r.deploy_mission(bad)
    good = mission("good", [{"type": "log", "text": "x"}], triggers=[{"id": "t", "type": "timer.interval", "seconds": 3600}])
    m, warnings = await harness.r.deploy_mission(good)
    assert m.sha256 and harness.r.triggers.armed_summary()["good"] == ["every 3600 s"]
    assert (harness.r.store.missions_dir / "good.json").exists()
    assert await harness.r.delete_mission("good")
    assert "good" not in harness.r.triggers.armed_summary() and "good" not in harness.r.store.names()


async def test_behavior_tree_files_are_written(harness):
    bt_dir = harness.r.store.bt_dir
    assert (bt_dir / "pickup_job__to_pickup.xml").exists()
    xml = (bt_dir / "pickup_job__to_pickup.xml").read_text("utf-8")
    assert 'number_of_retries="4"' in xml


async def test_missing_required_input_is_rejected(harness):
    ok, run, reason = await harness.r.run_mission("mqtt_goal_server", {}, RunSource("manual", "t"))
    assert not ok and "goal" in reason


async def test_http_api(harness):
    import aiohttp

    base = f"http://127.0.0.1:{harness.r.config.http_port}"
    async with aiohttp.ClientSession() as s:
        async with s.get(f"{base}/api/status") as resp:
            st = await resp.json()
            assert st["state"] == "idle" and st["runner"]["backend"] == "sim" and st["current_map"] == "demo_room"
        async with s.get(f"{base}/api/missions") as resp:
            names = {m["name"] for m in await resp.json()}
            assert "patrol" in names
        async with s.get(f"{base}/api/missions/patrol") as resp:
            doc = await resp.json()
            assert doc["name"] == "patrol"
        async with s.post(f"{base}/api/missions/validate", json={"schema": "mission/1", "name": "v", "flow": [{"type": "nope"}]}) as resp:
            v = await resp.json()
            assert not v["ok"] and v["errors"]
        async with s.put(f"{base}/api/missions/x", json={"schema": "mission/1", "name": "y", "flow": []}) as resp:
            assert resp.status == 400
        doc = mission("viahttp", [{"id": "l", "type": "log", "text": "hello ${who}"}], inputs={"who": {"type": "string", "default": "world"}})
        async with s.put(f"{base}/api/missions/viahttp", json=doc) as resp:
            assert resp.status == 200 and (await resp.json())["ok"]
        async with s.post(f"{base}/api/missions/viahttp/run", json={"inputs": {"who": "http"}}) as resp:
            r = await resp.json()
            assert r["accepted"] and r["run_id"]
        assert await harness.wait_idle()
        assert "hello http" in harness.logs()
        async with s.get(f"{base}/api/runs?limit=5") as resp:
            runs = await resp.json()
            assert runs[0]["mission"] == "viahttp" and runs[0]["status"] == "succeeded"
        async with s.get(f"{base}/api/runs/{runs[0]['id']}") as resp:
            detail = await resp.json()
            assert any(e["type"] == "log" for e in detail["events"])
        async with s.post(f"{base}/hooks/pickup", json={"pickup": "Conveyor1", "drop": "Rack3"}) as resp:
            assert resp.status == 202
        await asyncio.sleep(0.1)
        assert harness.r.dispatcher.current is not None and harness.r.dispatcher.current.mission == "pickup_job"
        async with s.post(f"{base}/api/stop") as resp:
            assert (await resp.json())["ok"]
        assert await harness.wait_idle()
        async with s.get(f"{base}/api/sites") as resp:
            sites = await resp.json()
        sites["maps"]["demo_room"]["sites"]["New"] = {"x": 1, "y": 2}
        async with s.put(f"{base}/api/sites", json=sites) as resp:
            assert (await resp.json())["ok"]
        assert harness.r.store.sites.lookup("demo_room", "New") is not None
        async with s.get(f"{base}/api/robot/pose") as resp:
            assert "yaw_deg" in await resp.json()
        async with s.get(f"{base}/api/capabilities") as resp:
            assert (await resp.json())["backend"] == "sim"
        async with s.get(f"{base}/") as resp:
            assert resp.status == 200 and "text/html" in resp.headers["Content-Type"]
        async with s.ws_connect(f"{base}/api/events") as ws:
            first = json.loads((await ws.receive()).data)
            assert first["type"] == "status"
            await ws.send_str('{"type":"ping"}')
            for _ in range(5):
                msg = json.loads((await ws.receive()).data)
                if msg["type"] == "pong":
                    break
            else:
                raise AssertionError("no pong")
        async with s.delete(f"{base}/api/missions/viahttp") as resp:
            assert (await resp.json())["ok"]


async def test_sim_topic_and_robot_endpoints(harness):
    import aiohttp

    base = f"http://127.0.0.1:{harness.r.config.http_port}"
    async with aiohttp.ClientSession() as s:
        async with s.post(f"{base}/api/sim/robot", json={"x": 5, "y": 6, "battery": 0.5}) as resp:
            body = await resp.json()
            assert body["robot"]["x"] == 5 and body["robot"]["battery"] == 0.5
        async with s.post(f"{base}/api/sim/topic", json={"topic": "/estop", "payload": {"data": True}}) as resp:
            assert (await resp.json())["listeners"] == 1  # global e-stop interrupt
        await asyncio.sleep(0.2)
        cur = harness.r.dispatcher.current
        assert cur is not None and cur.mission == "emergency_stop"
        p = harness.r.prompts.current
        assert p is not None
        harness.r.prompts.answer(p.id, "Resume")
        assert await harness.wait_idle()


async def test_map_endpoints(harness):
    import aiohttp

    base = f"http://127.0.0.1:{harness.r.config.http_port}"
    async with aiohttp.ClientSession() as s:
        async with s.get(f"{base}/api/maps/demo_room") as resp:
            meta = await resp.json()
            assert meta["name"] == "demo_room" and meta["source"] == "synthetic"
            assert meta["bounds"][0] < -1.5 and meta["bounds"][2] > 3.0
            assert meta["image_url"] == "/api/maps/demo_room/image"
        async with s.get(f"{base}{meta['image_url']}") as resp:
            body = await resp.read()
            assert resp.headers["Content-Type"] == "image/png" and body.startswith(b"\x89PNG")
        async with s.get(f"{base}/api/maps/nope") as resp:
            assert resp.status == 404


async def test_preview_route(harness):
    from mission_runner.preview import plan_route

    m = harness.r.store.get("patrol")
    out = await plan_route(harness.r, m)
    assert [leg["step_id"] for leg in out["legs"]] == ["lap"] * 4 + ["home"]
    assert all(leg["planned"] for leg in out["legs"]), [leg["error"] for leg in out["legs"]]
    assert out["distance_m"] > 4 and out["estimate_s"] > out["distance_m"] / out["speed_mps"] - 1
    assert out["legs"][0]["goal"]["x"] == 1.0 and out["legs"][0]["path"]["poses"]
    # site that only exists in another map still yields a leg with an error, not a crash
    doc = mission("faraway", [{"id": "g", "type": "nav.go_to_pose", "pose": "Inbound"}])
    from mission_runner.model import load_mission

    out2 = await plan_route(harness.r, load_mission(doc))
    assert out2["legs"] == [] or not out2["legs"][0]["planned"]


async def test_dry_run_covers_branches_and_prompts(harness):
    from mission_runner.model import load_mission
    from mission_runner.preview import DryRun

    doc = mission(
        "preview_me",
        [
            {"id": "w", "type": "nav.wait_active"},
            {"id": "loop", "type": "loop", "count": 2, "body": [{"id": "go", "type": "nav.go_to_pose", "pose": "A"}, {"id": "wait", "type": "wait", "seconds": 60}]},
            {"id": "ask", "type": "ask_user", "text": "again?", "options": ["Yes", "No"], "default": "No", "out": "a"},
            {"id": "pub", "type": "mqtt.publish", "connector": "broker", "topic": "robot/state", "payload": "done"},
            {"id": "ev", "type": "wait_event", "source": {"type": "http.webhook", "path": "never"}, "timeout_s": 5},
            {"id": "end", "type": "nav.go_to_pose", "pose": "Home"},
        ],
    )
    result = await DryRun(harness.r, load_mission(doc), time_scale=200.0, max_wall_s=25.0).execute()
    assert result.ok, result.error
    ids = [s["id"] for s in result.steps]
    assert ids.count("go") == 2 and "end" in ids
    assert result.distance_m > 2 and result.duration_s > 60  # the two 60 s waits are simulated, not slept
    assert len(result.samples) > 5 and {"t", "x", "y", "yaw_deg", "step"} <= set(result.samples[0])
    assert any("answered" in n for n in result.notes) and any("fires immediately" in n for n in result.notes)
    assert {"kind": "mqtt.publish", "topic": "robot/state", "payload": "done"} in result.outputs
    # the real robot never moved
    assert harness.r.backend.odometer == 0


async def test_dry_run_reports_failure_without_touching_the_robot(harness):
    from mission_runner.backends.base import Pose
    from mission_runner.model import load_mission
    from mission_runner.preview import DryRun

    harness.r.backend.fail_at = [Pose(1.0, 1.0)]
    doc = mission("will_fail", [{"id": "g", "type": "nav.go_to_pose", "pose": "A"}])
    result = await DryRun(harness.r, load_mission(doc), time_scale=100.0).execute()
    # the dry run has its own simulated robot, so the real one's fault injection does not apply
    assert result.ok
    harness.r.backend.fail_at = []


async def test_zones_expression_and_filters(harness):
    import aiohttp

    base = f"http://127.0.0.1:{harness.r.config.http_port}"
    async with aiohttp.ClientSession() as s:
        async with s.get(f"{base}/api/sites") as resp:
            sites = await resp.json()
        sites["maps"]["demo_room"]["zones"] = {
            "no_go": {"kind": "keepout", "polygon": [[2, 2], [4, 2], [4, 4], [2, 4]]},
            "slow": {"kind": "speed_limit", "speed_mps": 0.2, "polygon": [[-2, -2], [0, -2], [0, 0], [-2, 0]]},
        }
        async with s.put(f"{base}/api/sites", json=sites) as resp:
            assert (await resp.json())["ok"]
        async with s.post(f"{base}/api/maps/demo_room/filters", json={"max_speed_mps": 0.6}) as resp:
            out = await resp.json()
        kinds = {m["kind"] for m in out["masks"]}
        assert kinds == {"keepout", "speed_limit"}
        from pathlib import Path

        for m in out["masks"]:
            assert Path(m["image"]).read_bytes().startswith(b"P5")
            text = Path(m["yaml"]).read_text()
            assert "mode: trinary" in text or "mode: percent" in text

    zone = harness.r.store.sites.map("demo_room").zones["no_go"]
    assert zone.contains(3, 3) and not zone.contains(0, 0)

    doc = mission("zoned", [
        {"id": "s", "type": "set", "var": "here", "value": "${zone_of()}"},
        {"id": "i", "type": "if", "condition": "in_zone('no_go', 3, 3)", "then": [{"id": "l", "type": "log", "text": "inside"}]},
    ])
    await harness.r.deploy_mission(doc)
    run = await harness.run("zoned")
    assert await harness.wait_idle()
    assert run.status == RunStatus.SUCCEEDED, run.error
    assert "inside" in harness.logs(run.id)


async def test_live_layers_over_websocket(harness):
    import aiohttp

    base = f"http://127.0.0.1:{harness.r.config.http_port}"
    async with aiohttp.ClientSession() as s, s.ws_connect(f"{base}/api/events") as ws:
        assert json.loads((await ws.receive()).data)["type"] == "status"
        await ws.send_str(json.dumps({"type": "live", "layers": ["footprint", "costmap"]}))
        deadline = asyncio.get_running_loop().time() + 6
        got_ack = got_footprint = False
        while asyncio.get_running_loop().time() < deadline and not (got_ack and got_footprint):
            msg = json.loads((await ws.receive()).data)
            if msg["type"] == "live.layers":
                got_ack = True
                assert msg["available"]["footprint"] is True and msg["available"]["costmap"] is False
            elif msg["type"] == "live.footprint":
                got_footprint = True
                assert len(msg["points"]) == 4 and msg["frame"] == "map"
        assert got_ack and got_footprint
        assert harness.r.live.layers == {"footprint", "costmap"}
    await asyncio.sleep(0.2)
    assert harness.r.live.layers == set()  # unsubscribed when the client went away


async def test_preview_endpoints_over_http(harness):
    import aiohttp

    base = f"http://127.0.0.1:{harness.r.config.http_port}"
    async with aiohttp.ClientSession() as s:
        async with s.post(f"{base}/api/preview/route", json={"name": "patrol"}) as resp:
            out = await resp.json()
            assert out["ok"] and len(out["legs"]) == 5
        unsaved = mission("unsaved", [{"id": "g", "type": "nav.go_to_pose", "pose": {"x": 2, "y": 2}}])
        async with s.post(f"{base}/api/preview/route", json={"mission": unsaved}) as resp:
            out = await resp.json()
            assert out["ok"] and out["legs"][0]["goal"]["x"] == 2
        async with s.post(f"{base}/api/preview/dryrun", json={"name": "go_to_point", "time_scale": 100}) as resp:
            out = await resp.json()
            assert out["ok"] and out["samples"] and out["steps"][0]["id"] == "wait"
        async with s.post(f"{base}/api/preview/route", json={"name": "nope"}) as resp:
            assert resp.status == 404


async def test_follow_route_stays_on_the_graph(harness):
    doc = mission("routed", [
        {"id": "w", "type": "nav.wait_active"},
        {"id": "go", "type": "nav.follow_route", "to": "Rack3", "out": "r"},
    ])
    await harness.r.deploy_mission(doc)
    run = await harness.run("routed")
    assert await harness.wait_idle(90)
    assert run.status == RunStatus.SUCCEEDED, run.error
    value = [e["result"]["value"] for e in harness.events if e["type"] == "step.finished" and e["step_id"] == "go"][0]
    assert value["route"] == ["Home", "A", "B", "Conveyor1", "Rack3"]
    assert value["length_m"] > 7
    robot = harness.r.robot_scope()
    assert abs(robot["x"] - 3.0) < 1e-6 and abs(robot["y"] + 3.0) < 1e-6
    # driving the lanes is longer than the straight line, which is the point
    assert harness.r.backend.odometer > 7.0


async def test_follow_route_without_a_path(harness):
    doc = mission("noroute", [{"id": "go", "type": "nav.follow_route", "to": "Rack3", "from": "Charger"}])
    sites = harness.r.store.sites.to_dict()
    sites["maps"]["demo_room"]["edges"] = [{"from": "Home", "to": "Charger"}]
    harness.r.store.save_sites(sites)
    await harness.r.deploy_mission(doc)
    run = await harness.run("noroute")
    assert await harness.wait_idle()
    assert run.status == RunStatus.FAILED and "no route" in run.error

    doc2 = mission("direct", [{"id": "go", "type": "nav.follow_route", "to": "Rack3", "from": "Charger", "on_no_route": "direct"}])
    await harness.r.deploy_mission(doc2)
    run = await harness.run("direct")
    assert await harness.wait_idle(60)
    assert run.status == RunStatus.SUCCEEDED, run.error


async def test_follow_route_applies_edge_speed_limits(harness):
    sites = harness.r.store.sites.to_dict()
    sites["maps"]["demo_room"]["edges"] = [{"from": "Home", "to": "A", "speed_mps": 0.2}, {"from": "A", "to": "B"}]
    harness.r.store.save_sites(sites)
    doc = mission("slow", [{"id": "go", "type": "nav.follow_route", "to": "B", "apply_speed_limits": True}])
    await harness.r.deploy_mission(doc)
    run = await harness.run("slow")
    assert await harness.wait_idle(90)
    assert run.status == RunStatus.SUCCEEDED, run.error
    names = [(node, params) for node, params in harness.r.backend.params_set]
    assert names and names[0][0] == "/controller_server" and names[0][1]["FollowPath.max_vel_x"] == 0.2
    assert names[-1][1]["FollowPath.max_vel_x"] == 0.5  # restored


def _record_nav(harness) -> list:
    """Wrap the sim backend so a test sees every nav / param call, in order."""
    b = harness.r.backend
    calls: list = []
    orig = {n: getattr(b, n) for n in ("go_to_pose", "go_through_poses", "follow_path", "set_params")}

    async def go_to_pose(pose, bt, fb):
        calls.append(("go_to_pose", [pose]))
        return await orig["go_to_pose"](pose, bt, fb)

    async def go_through_poses(poses, bt, fb):
        calls.append(("through_poses", list(poses)))
        return await orig["go_through_poses"](poses, bt, fb)

    async def follow_path(path, controller_id, goal_checker_id, fb):
        calls.append(("follow_path", path, controller_id, goal_checker_id))
        return await orig["follow_path"](path, controller_id, goal_checker_id, fb)

    async def set_params(node, params):
        calls.append(("param", params))
        return await orig["set_params"](node, params)

    b.go_to_pose, b.go_through_poses, b.follow_path, b.set_params = go_to_pose, go_through_poses, follow_path, set_params
    return calls


def _set_edges(harness, edges: list) -> None:
    sites = harness.r.store.sites.to_dict()
    sites["maps"]["demo_room"]["edges"] = edges
    harness.r.store.save_sites(sites)


def _value(harness, step_id: str = "go") -> dict:
    return [e["result"]["value"] for e in harness.events if e["type"] == "step.finished" and e["step_id"] == step_id][-1]


async def test_follow_route_densifies_lanes(harness):
    # Home (0,0) -> A (1,1): 1.414 m; A -> B (1,0.5): 0.5 m; B yaw -180
    _set_edges(harness, [{"from": "Home", "to": "A"}, {"from": "A", "to": "B"}])
    calls = _record_nav(harness)
    await harness.r.deploy_mission(mission("dense", [{"id": "go", "type": "nav.follow_route", "from": "Home", "to": "B", "waypoint_spacing_m": 0.5}]))
    run = await harness.run("dense")
    assert await harness.wait_idle(60)
    assert run.status == RunStatus.SUCCEEDED, run.error
    assert [c[0] for c in calls] == ["through_poses"]
    poses = calls[0][1]
    # 0.5 and 1.0 along Home->A, node A, then node B (A->B is exactly one spacing long)
    assert len(poses) == 4
    assert [round(p.yaw_deg) for p in poses] == [45, 45, -90, -180]
    assert abs(poses[0].x - 0.5 / 2**0.5) < 1e-9 and (poses[2].x, poses[2].y) == (1.0, 1.0) and (poses[3].x, poses[3].y) == (1.0, 0.5)
    value = _value(harness)
    assert value["waypoint_spacing_m"] == 0.5
    assert value["segments"] == [{"mode": "through_poses", "sites": ["Home", "A", "B"], "poses": 4}]
    robot = harness.r.robot_scope()
    assert abs(robot["x"] - 1.0) < 1e-6 and abs(robot["y"] - 0.5) < 1e-6 and abs(abs(robot["yaw_deg"]) - 180) < 1e-6


async def test_follow_route_spacing_zero_sends_only_nodes(harness):
    _set_edges(harness, [{"from": "Home", "to": "A"}, {"from": "A", "to": "B"}])
    calls = _record_nav(harness)
    await harness.r.deploy_mission(mission("nodes", [{"id": "go", "type": "nav.follow_route", "from": "Home", "to": "B", "waypoint_spacing_m": 0}]))
    run = await harness.run("nodes")
    assert await harness.wait_idle(60)
    assert run.status == RunStatus.SUCCEEDED, run.error
    assert [(p.x, p.y) for p in calls[0][1]] == [(1.0, 1.0), (1.0, 0.5)]
    assert _value(harness)["segments"] == [{"mode": "through_poses", "sites": ["Home", "A", "B"], "poses": 2}]


async def test_follow_route_strict_lane_in_the_middle(harness):
    _set_edges(harness, [
        {"from": "Home", "to": "A"}, {"from": "A", "to": "B", "strict": True},
        {"from": "B", "to": "Conveyor1"}, {"from": "Conveyor1", "to": "Rack3"},
    ])
    calls = _record_nav(harness)
    await harness.r.deploy_mission(mission("strict", [{"id": "go", "type": "nav.follow_route", "from": "Home", "to": "Rack3", "controller_id": "Exact"}]))
    run = await harness.run("strict")
    assert await harness.wait_idle(90)
    assert run.status == RunStatus.SUCCEEDED, run.error
    value = _value(harness)
    assert [(s["mode"], s["sites"]) for s in value["segments"]] == [
        ("through_poses", ["Home", "A"]), ("follow_path", ["A", "B"]), ("through_poses", ["B", "Conveyor1", "Rack3"]),
    ]
    assert "lead_in" not in value and value["waypoint_spacing_m"] == 0.75
    assert [c[0] for c in calls] == ["through_poses", "follow_path", "through_poses"]
    # the node before the strict lane faces along it
    assert round(calls[0][1][-1].yaw_deg) == -90
    _, path, controller_id, goal_checker_id = calls[1]
    assert controller_id == "Exact" and goal_checker_id == ""
    pts = path["poses"]
    assert path["frame"] == "map" and len(pts) == value["segments"][1]["poses"] > 5
    assert all(abs(p["x"] - 1.0) < 1e-9 and round(p["yaw_deg"]) == -90 for p in pts)
    assert (pts[0]["y"], pts[-1]["y"]) == (1.0, 0.5)
    robot = harness.r.robot_scope()
    assert abs(robot["x"] - 3.0) < 1e-6 and abs(robot["y"] + 3.0) < 1e-6 and abs(robot["yaw_deg"] + 90) < 1e-6


async def test_follow_route_strict_first_lane_drives_to_its_start(harness):
    from mission_runner.backends.base import Pose

    _set_edges(harness, [{"from": "Home", "to": "A", "strict": True}])
    harness.r.backend.set_pose(Pose(-1.0, 0.0, 0.0))
    calls = _record_nav(harness)
    await harness.r.deploy_mission(mission("lead", [{"id": "go", "type": "nav.follow_route", "from": "Home", "to": "A"}]))
    run = await harness.run("lead")
    assert await harness.wait_idle(60)
    assert run.status == RunStatus.SUCCEEDED, run.error
    assert [c[0] for c in calls] == ["go_to_pose", "follow_path"]
    assert (calls[0][1][0].x, calls[0][1][0].y, round(calls[0][1][0].yaw_deg)) == (0.0, 0.0, 45)
    value = _value(harness)
    assert value["lead_in"] == "Home"
    assert [s["mode"] for s in value["segments"]] == ["go_to_pose", "follow_path"]
    assert calls[1][1]["poses"][-1]["yaw_deg"] == 0  # A's own yaw at the destination
    robot = harness.r.robot_scope()
    assert abs(robot["x"] - 1.0) < 1e-6 and abs(robot["y"] - 1.0) < 1e-6


async def test_follow_route_speed_caps_per_lane_with_strict(harness):
    _set_edges(harness, [
        {"from": "Home", "to": "A", "speed_mps": 0.2}, {"from": "A", "to": "B"},
        {"from": "B", "to": "C", "speed_mps": 0.3, "strict": True},
    ])
    calls = _record_nav(harness)
    await harness.r.deploy_mission(mission("caps", [{"id": "go", "type": "nav.follow_route", "from": "Home", "to": "C", "apply_speed_limits": True}]))
    run = await harness.run("caps")
    assert await harness.wait_idle(90)
    assert run.status == RunStatus.SUCCEEDED, run.error
    seq = [c[1]["FollowPath.max_vel_x"] if c[0] == "param" else c[0] for c in calls]
    assert seq == [0.2, "through_poses", 0.5, "go_to_pose", 0.3, "follow_path", 0.5]
    assert [s["sites"] for s in _value(harness)["segments"]] == [["Home", "A"], ["A", "B"], ["B", "C"]]


async def test_edge_strict_in_project_export(harness):
    _set_edges(harness, [{"from": "Home", "to": "A", "strict": True}, {"from": "A", "to": "B"}])
    edges = harness.r.export_project()["sites"]["maps"]["demo_room"]["edges"]
    assert edges == [{"from": "Home", "to": "A", "strict": True}, {"from": "A", "to": "B"}]


async def test_call_api_tunnel(harness):
    """The ROS /mission/api service forwards to this; a GUI on foxglove_bridge
    reaches every endpoint through it."""
    status, body = await harness.r.call_api("GET", "/api/status")
    assert status == 200 and body["state"] == "idle"
    status, body = await harness.r.call_api("GET", "/api/missions")
    assert status == 200 and any(m["name"] == "patrol" for m in body)
    status, body = await harness.r.call_api("POST", "/api/missions/go_to_point/run", {"inputs": {}})
    assert status == 200 and body["accepted"]
    assert await harness.wait_idle()
    status, body = await harness.r.call_api("GET", "/api/maps/demo_room/image")
    assert status == 200 and body["content_type"] == "image/png" and body["base64"].startswith("iVBOR")
    status, body = await harness.r.call_api("GET", "/api/missions/nope")
    assert status == 404 and "error" in body


async def test_connector_status_lists_referenced_names(harness):
    """A mission is written before the broker exists; the editor still needs the
    name to offer, marked as not configured."""
    status = harness.r.connector_status()
    assert status["broker"]["configured"] is False and "not configured" in status["broker"]["reason"]
    assert status["plc1"]["configured"] is False
    status, body = await harness.r.call_api("GET", "/api/connectors")
    assert status == 200 and "broker" in body
