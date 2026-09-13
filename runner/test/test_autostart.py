"""Autostart services (docs/robot-startup.md section 4), against a fake systemd."""

import asyncio
import json
import os
import shlex

import pytest

from mission_runner import autostart as autostart_mod
from mission_runner.autostart import AutostartError, AutostartManager, CommandResult, is_launch_file, parse_show, parse_timestamp


class FakeSystemd:
    """Answers systemctl --user / journalctl / loginctl like a small user manager."""

    def __init__(self):
        self.calls: list[list[str]] = []
        self.units: dict[str, dict] = {}
        self.linger = False
        self.allow_linger = False

    def unit(self, name: str) -> dict:
        return self.units.setdefault(name, {"active": "inactive", "enabled": False, "restarts": 0, "pid": 0})

    def __call__(self, argv, env, timeout):
        self.calls.append(list(argv))
        assert env.get("XDG_RUNTIME_DIR") or not hasattr(os, "getuid")
        if argv[0] == "systemctl":
            assert argv[1] == "--user"
            a = [x for x in argv[2:] if x != "--no-block"]
            verb = a[0]
            if verb in ("show-environment", "daemon-reload"):
                return CommandResult(0, "HOME=/home/pi\n")
            u = self.unit(a[1])
            if verb == "enable":
                u["enabled"] = True
            elif verb == "disable":
                u["enabled"] = False
            elif verb in ("start", "restart"):
                u.update(active="active", pid=4242)
            elif verb == "stop":
                u.update(active="inactive", pid=0)
            elif verb == "show":
                text = (
                    f"ActiveState={u['active']}\nSubState={'running' if u['active'] == 'active' else 'dead'}\n"
                    f"UnitFileState={'enabled' if u['enabled'] else 'disabled'}\n"
                    f"ActiveEnterTimestamp={'Sun 2026-09-13 08:02:11 +07' if u['active'] == 'active' else 'n/a'}\n"
                    f"NRestarts={u['restarts']}\nMainPID={u['pid']}\n"
                )
                return CommandResult(0, text)
            return CommandResult(0)
        if argv[0] == "journalctl":
            n = int(argv[argv.index("-n") + 1])
            return CommandResult(0, "".join(f"2026-09-13T08:02:{i:02d}+0700 pi robot.sh[1]: line {i}\n" for i in range(min(n, 3))))
        if argv[0] == "loginctl":
            if argv[1] == "show-user":
                return CommandResult(0, f"Linger={'yes' if self.linger else 'no'}\n")
            if argv[1] == "enable-linger":
                if self.allow_linger:
                    self.linger = True
                    return CommandResult(0)
                return CommandResult(1, "", "Access denied")
        return CommandResult(127, "", "unknown")

    def verbs(self) -> list[str]:
        return [" ".join(x for x in c[2:] if not x.startswith("-p") and "State" not in x) for c in self.calls if c[0] == "systemctl" and c[2] not in ("show", "show-environment")]


class Env:
    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.user_home = tmp_path / "pi"
        self.ros = tmp_path / "opt_ros"
        (self.ros / "jazzy").mkdir(parents=True)
        (self.ros / "jazzy" / "setup.bash").write_text("# ros\n")
        self.ws = self.user_home / "robot_ws"
        (self.ws / "install").mkdir(parents=True)
        (self.ws / "install" / "setup.bash").write_text("# ws\n")
        self.launch = self.ws / "src" / "my_robot" / "launch" / "robot.launch.py"
        self.launch.parent.mkdir(parents=True)
        self.launch.write_text("# launch\n")
        self.cgroup = tmp_path / "cgroup"
        self.cgroup.write_text("0::/user.slice/user-1000.slice/user@1000.service/app.slice/something.service\n")
        self.fake = FakeSystemd()

    def manager(self, **kw) -> AutostartManager:
        opts = dict(run=self.fake, user_home=self.user_home, environ={"XDG_RUNTIME_DIR": "/run/user/1000"}, platform="linux", ros_root=self.ros, cgroup_file=self.cgroup, user="pi")
        config = kw.pop("config", {"roots": ["~", str(self.ros)]})
        opts.update(kw)
        return AutostartManager(self.user_home / ".mission", config, **opts)


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def real(p) -> str:
    return os.path.realpath(p)


def errors_of(fn, *args, **kw) -> list[str]:
    with pytest.raises(AutostartError) as ei:
        fn(*args, **kw)
    return ei.value.errors


# ----- validation ------------------------------------------------------------------------------------


def test_names_are_strict(env):
    mgr = env.manager()
    body = {"launch": {"file": str(env.launch)}}
    for bad in ["Robot", "1robot", "a" * 33, "a_b", "a b", "", "robot\n", "../x", "browse", "linger", "-x"]:
        with pytest.raises(AutostartError) as ei:
            mgr.validate(bad, body)
        assert ei.value.status == 400, bad
    assert mgr.validate("a" * 32, body)["name"] == "a" * 32
    assert mgr.validate("robot-2", body)["launch"] == {"file": real(env.launch)}


def test_file_targets_must_be_launch_files_under_the_roots(env, tmp_path):
    mgr = env.manager()
    outside = tmp_path / "outside" / "launch" / "evil.launch.py"
    outside.parent.mkdir(parents=True)
    outside.write_text("")
    notes = env.user_home / "notes.txt"
    notes.write_text("")
    loose_py = env.ws / "src" / "tool.py"
    loose_py.write_text("")
    in_launch_dir = env.launch.parent / "plain.py"
    in_launch_dir.write_text("")

    def bad(file):
        return errors_of(mgr.validate, "robot", {"launch": {"file": file}})

    assert "outside the allowed roots" in bad(str(outside))[0]
    assert "outside the allowed roots" in bad(str(env.user_home / ".." / "outside" / "launch" / "evil.launch.py"))[0]
    assert "does not exist" in bad(str(env.launch.parent / "missing.launch.py"))[0]
    assert "not a launch file" in bad(str(notes))[0]
    assert "not a launch file" in bad(str(loose_py))[0]
    assert "absolute" in bad("robot_ws/src/my_robot/launch/robot.launch.py")[0]
    assert "absolute" in bad(str(env.launch) + "\n")[0]
    assert mgr.validate("robot", {"launch": {"file": str(in_launch_dir)}})["launch"]["file"] == real(in_launch_dir)
    assert errors_of(mgr.validate, "robot", {"launch": "robot.launch.py"})
    assert errors_of(mgr.validate, "robot", {})


def test_symlink_out_of_the_roots_is_rejected(env, tmp_path):
    mgr = env.manager()
    target = tmp_path / "elsewhere" / "x.launch.py"
    target.parent.mkdir()
    target.write_text("")
    link = env.user_home / "linked.launch.py"
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not permitted here")
    assert "outside the allowed roots" in errors_of(mgr.validate, "robot", {"launch": {"file": str(link)}})[0]


def test_package_target_names_only(env):
    mgr = env.manager()
    ok = mgr.validate("mission", {"launch": {"package": "mission_runner", "file": "bringup.launch.py"}})
    assert ok["launch"] == {"package": "mission_runner", "file": "bringup.launch.py"}
    for pkg, file in [("mission_runner", "../x.launch.py"), ("a b", "x.py"), ("-h", "x.py"), ("pkg", "--debug"), ("pkg;rm", "x.py"), ("..", "x.py"), ("pkg", "x.py\n")]:
        assert errors_of(mgr.validate, "mission", {"launch": {"package": pkg, "file": file}}), (pkg, file)


def test_args_and_values_are_checked_and_quoted(env):
    mgr = env.manager()
    for bad in ["a b:=c", "$(reboot):=1", "-x:=1", "novalue", "x:=one\ntwo", "x:=a\rb", 7]:
        assert errors_of(mgr.validate, "robot", {"launch": {"file": str(env.launch)}, "args": [bad]}), bad
    spec = mgr.validate("robot", {"launch": {"file": str(env.launch)}, "args": ["a:=b; rm -rf /", "q:=$(reboot) `id` 'x' \"y\"", "empty:="]})
    wrapper = mgr.render_wrapper(spec)
    exec_line = [line for line in wrapper.splitlines() if line.startswith("exec ")][0]
    assert shlex.split(exec_line) == ["exec", "ros2", "launch", real(env.launch), "a:=b; rm -rf /", "q:=$(reboot) `id` 'x' \"y\"", "empty:="]
    for field, value in [("ros_domain_id", "7"), ("ros_domain_id", 233), ("ros_domain_id", True), ("rmw", "rmw_x; reboot"), ("rmw", "cyclone"), ("description", "two\nlines"), ("after", ["nope"]), ("after", ["robot"]), ("start_now", "yes"), ("workspaces", ["/etc/passwd"]), ("workspaces", "x")]:
        assert errors_of(mgr.validate, "robot", {"launch": {"file": str(env.launch)}, field: value}), (field, value)


def test_missing_ros_distro_is_an_error(env, tmp_path):
    mgr = env.manager(ros_root=tmp_path / "no_ros")
    assert any("no ROS 2 distribution" in e for e in errors_of(mgr.validate, "robot", {"launch": {"file": str(env.launch)}}))
    mgr = env.manager(environ={"ROS_DISTRO": "rolling"})
    assert any("does not exist" in e for e in errors_of(mgr.validate, "robot", {"launch": {"file": str(env.launch)}}))


# ----- rendering ---------------------------------------------------------------------------------------


def test_wrapper_and_unit_text(env):
    mgr = env.manager()
    mgr.put("robot", {"launch": {"file": str(env.launch)}})
    spec = mgr.validate(
        "mission",
        {"description": "Mission layer 100%", "launch": {"package": "mission_runner", "file": "bringup.launch.py"}, "args": ["sim:=true"], "ros_domain_id": 7, "rmw": "rmw_cyclonedds_cpp", "after": ["robot"], "workspaces": [str(env.ws / "install" / "setup.bash")]},
    )
    q = shlex.quote
    assert mgr.render_wrapper(spec) == (
        "#!/bin/bash\n"
        "# Written by mission_runner autostart. Edit through Mission Builder or `mission_runner autostart`.\n"
        "set -e\n"
        f"source {q(str(env.ros / 'jazzy' / 'setup.bash'))}\n"
        f"source {q(real(env.ws / 'install' / 'setup.bash'))}\n"
        "export ROS_DOMAIN_ID=7\n"
        "export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp\n"
        "exec ros2 launch mission_runner bringup.launch.py sim:=true\n"
    )
    unit = mgr.render_unit(spec)
    lines = unit.splitlines()
    assert lines[0] == "[Unit]"
    assert "Description=Mission layer 100%% (mission_runner autostart)" in lines
    assert "After=network-online.target mission-autostart-robot.service" in lines
    assert "Wants=network-online.target mission-autostart-robot.service" in lines
    for expected in ["StartLimitIntervalSec=0", "Type=simple", "Restart=on-failure", "RestartSec=5", "KillSignal=SIGINT", "TimeoutStopSec=30", "WantedBy=default.target"]:
        assert expected in lines
    exec_start = [x for x in lines if x.startswith("ExecStart=")][0]
    assert exec_start.rstrip('"').endswith("mission.sh")


def test_workspace_detection(env):
    outer = env.user_home / "outer"
    inner = outer / "src" / "inner_ws"
    for ws in (outer, inner):
        (ws / "install").mkdir(parents=True)
        (ws / "install" / "setup.bash").write_text("")
    launch = inner / "src" / "pkg" / "launch" / "a.launch.xml"
    launch.parent.mkdir(parents=True)
    launch.write_text("")
    mgr = env.manager()
    assert mgr.validate("robot", {"launch": {"file": str(launch)}})["workspaces"] == [real(outer / "install" / "setup.bash"), real(inner / "install" / "setup.bash")]
    assert mgr.validate("robot", {"launch": {"file": str(env.launch)}})["workspaces"] == [real(env.ws / "install" / "setup.bash")]
    # package target: the workspaces the runner was started from, overlay last
    mgr = env.manager(environ={"COLCON_PREFIX_PATH": os.pathsep.join([str(inner / "install"), str(outer / "install")])})
    assert mgr.validate("mission", {"launch": {"package": "mission_runner", "file": "bringup.launch.py"}})["workspaces"] == [real(outer / "install" / "setup.bash"), real(inner / "install" / "setup.bash")]
    # explicit workspaces win
    assert mgr.validate("robot", {"launch": {"file": str(launch)}, "workspaces": [str(outer / "install" / "setup.bash")]})["workspaces"] == [real(outer / "install" / "setup.bash")]


def test_is_launch_file():
    assert is_launch_file("/a/b/robot.launch.py") and is_launch_file("/a/x.launch.xml") and is_launch_file("/a/x.launch.yaml")
    assert is_launch_file("/ws/launch/nav.py") and is_launch_file("/ws/launch/nav.yaml")
    assert not is_launch_file("/ws/src/nav.py") and not is_launch_file("/ws/launch/readme.md") and not is_launch_file("/a/x.launch")


def test_parse_show_and_timestamps():
    props = parse_show("ActiveState=active\nSubState=running\nUnitFileState=enabled\nActiveEnterTimestamp=Sun 2026-09-13 08:02:11 +07\nNRestarts=3\nMainPID=812\n")
    assert props["ActiveState"] == "active" and props["NRestarts"] == "3" and props["ActiveEnterTimestamp"].startswith("Sun 2026")
    assert parse_timestamp(props["ActiveEnterTimestamp"]).startswith("2026-09-13T08:02:11")
    assert parse_timestamp("@1757750531").startswith("2025-09-13T")
    assert parse_timestamp("n/a") is None and parse_timestamp("") is None and parse_timestamp("garbage") is None


# ----- lifecycle --------------------------------------------------------------------------------------------


def test_put_list_actions_log_remove(env):
    mgr = env.manager()
    svc = mgr.put("robot", {"launch": {"file": str(env.launch)}, "args": ["use_sim_time:=false"], "start_now": True})
    unit, wrapper, spec = mgr.paths("robot")
    assert unit.name == "mission-autostart-robot.service" and unit.parent == env.user_home / ".config" / "systemd" / "user"
    assert wrapper == env.user_home / ".mission" / "autostart" / "robot.sh" and spec.name == "robot.json"
    assert unit.exists() and wrapper.exists() and spec.exists()
    assert json.loads(spec.read_text())["args"] == ["use_sim_time:=false"]
    assert env.fake.verbs() == ["daemon-reload", "enable mission-autostart-robot.service", "restart mission-autostart-robot.service"]
    assert svc["active"] == "active" and svc["enabled"] and svc["main_pid"] == 4242 and svc["since"].startswith("2026-09-13T08:02:11")
    assert svc["restarts"] == 0 and svc["sub_state"] == "running" and svc["self"] is False
    assert set(svc) == {"name", "unit", "description", "launch", "args", "workspaces", "ros_domain_id", "rmw", "after", "enabled", "active", "sub_state", "since", "restarts", "main_pid", "self"}

    mgr.put("mission", {"launch": {"package": "mission_runner", "file": "bringup.launch.py"}, "after": ["robot"]})
    ov = mgr.overview()
    assert ov["supported"] and ov["enabled"] and ov["user"] == "pi" and ov["linger"] is False and ov["ros_distro"] == "jazzy"
    assert [s["name"] for s in ov["services"]] == ["mission", "robot"]
    assert ov["services"][0]["active"] == "inactive" and ov["services"][0]["since"] is None and ov["services"][0]["main_pid"] is None
    assert "reason" not in ov and "self" not in ov

    env.fake.calls.clear()
    assert mgr.action("robot", "stop")["active"] == "inactive"
    assert mgr.action("robot", "start")["active"] == "active"
    assert env.fake.verbs() == ["stop mission-autostart-robot.service", "start mission-autostart-robot.service"]
    assert len(mgr.log("robot", 2)["lines"]) == 2
    assert ["journalctl", "--user", "-u", "mission-autostart-robot.service", "-n", "2", "--no-pager", "-o", "short-iso"] in env.fake.calls
    assert errors_of(mgr.action, "ghost", "start") and errors_of(mgr.log, "ghost")

    env.fake.calls.clear()
    assert mgr.remove("robot") == {"removed": "robot", "self": False}
    assert not unit.exists() and not wrapper.exists() and not spec.exists()
    assert env.fake.verbs() == ["stop mission-autostart-robot.service", "disable mission-autostart-robot.service", "daemon-reload"]
    assert [s["name"] for s in mgr.list()] == ["mission"]
    with pytest.raises(AutostartError) as ei:
        mgr.remove("robot")
    assert ei.value.status == 404


def test_unsupported_lists_but_refuses_changes(env):
    mgr = env.manager()
    mgr.put("robot", {"launch": {"file": str(env.launch)}})
    off = env.manager(platform="win32")
    ov = off.overview()
    assert ov["supported"] is False and "Linux" in ov["reason"]
    assert [s["name"] for s in ov["services"]] == ["robot"] and ov["services"][0]["active"] == "unknown"
    for fn, args in [(off.put, ("robot", {"launch": {"file": str(env.launch)}})), (off.action, ("robot", "start")), (off.remove, ("robot",)), (off.log, ("robot",)), (off.enable_linger, ())]:
        with pytest.raises(AutostartError) as ei:
            fn(*args)
        assert ei.value.status == 409

    def no_user_manager(argv, env_, timeout):
        return CommandResult(1, "", "Failed to connect to bus: No medium found")

    ov = env.manager(run=no_user_manager).overview()
    assert ov["supported"] is False and "No medium found" in ov["reason"]


def test_self_detection_and_linger(env):
    mgr = env.manager(environ={"INVOCATION_ID": "abc"})
    assert mgr.self_name() is None
    env.cgroup.write_text("0::/user.slice/user-1000.slice/user@1000.service/app.slice/mission-autostart-mission.service\n")
    assert mgr.self_name() == "mission" and mgr.is_self("mission") and not mgr.is_self("robot")
    assert env.manager().self_name() is None  # no INVOCATION_ID: not started by systemd

    assert mgr.enable_linger() == {"linger": False, "command": "sudo loginctl enable-linger pi"}
    env.fake.allow_linger = True
    assert mgr.enable_linger() == {"linger": True}


def test_browse(env, tmp_path):
    mgr = env.manager()
    (env.user_home / ".hidden").mkdir()
    (env.user_home / "b.launch.py").write_text("")
    (env.user_home / "A.txt").write_text("")
    out = mgr.browse()
    assert out["path"] == real(env.user_home) and out["parent"] is None
    assert out["roots"] == [real(env.user_home), real(env.ros)]
    names = [(e["name"], e["kind"], e["launch"]) for e in out["entries"]]
    assert names == [("robot_ws", "dir", False), ("A.txt", "file", False), ("b.launch.py", "file", True)]
    sub = mgr.browse(str(env.launch.parent))
    assert sub["parent"] == real(env.launch.parent.parent)
    assert sub["entries"] == [{"name": "robot.launch.py", "path": os.path.join(real(env.launch.parent), "robot.launch.py"), "kind": "file", "launch": True}]
    for path, status in [(str(tmp_path), 403), (str(env.user_home / ".." / ".."), 403), (str(env.launch), 400), ("relative/dir", 400)]:
        with pytest.raises(AutostartError) as ei:
            mgr.browse(path)
        assert ei.value.status == status, path
    try:
        os.symlink(tmp_path, env.user_home / "escape", target_is_directory=True)
    except (OSError, NotImplementedError):
        return
    assert "escape" not in [e["name"] for e in mgr.browse()["entries"]]


# ----- CLI ----------------------------------------------------------------------------------------------------


def test_cli_autostart(env, monkeypatch, capsys):
    from mission_runner.cli import main

    mgr = env.manager()
    monkeypatch.setattr(autostart_mod, "AutostartManager", lambda home, config: mgr)
    home = str(env.user_home / ".mission")
    assert main(["autostart", "add", "robot", "--launch", str(env.launch), "--arg", "use_sim_time:=false", "--home", home]) == 0
    assert main(["autostart", "add", "mission", "--package", "mission_runner", "--launch", "bringup.launch.py", "--after", "robot", "--domain", "7", "--arg", "sim:=true", "--start", "--home", home]) == 0
    spec = json.loads(mgr.paths("mission")[2].read_text())
    assert spec["ros_domain_id"] == 7 and spec["after"] == ["robot"] and spec["args"] == ["sim:=true"]
    assert "export ROS_DOMAIN_ID=7" in mgr.paths("mission")[1].read_text()
    # re-adding changes only what is given
    assert main(["autostart", "add", "mission", "--rmw", "rmw_cyclonedds_cpp", "--start", "--home", home]) == 0
    spec = json.loads(mgr.paths("mission")[2].read_text())
    assert spec["ros_domain_id"] == 7 and spec["args"] == ["sim:=true"] and spec["rmw"] == "rmw_cyclonedds_cpp"
    capsys.readouterr()
    assert main(["autostart", "list", "--home", home]) == 0
    out = capsys.readouterr().out
    assert "mission" in out and "starts after robot" in out and "linger OFF" in out
    assert main(["autostart", "add", "bad", "--launch", str(env.user_home / "nope.launch.py"), "--home", home]) == 1
    assert "does not exist" in capsys.readouterr().out
    assert main(["autostart", "stop", "mission", "--home", home]) == 0
    assert main(["autostart", "log", "mission", "--lines", "1", "--home", home]) == 0
    assert main(["autostart", "remove", "mission", "--home", home]) == 0
    assert not mgr.paths("mission")[0].exists()
    assert main(["autostart", "linger", "--home", home]) == 1
    assert "sudo loginctl enable-linger pi" in capsys.readouterr().out


# ----- HTTP ------------------------------------------------------------------------------------------------------


async def test_http_autostart(harness, env):
    import aiohttp

    mgr = env.manager()
    harness.r.autostart = mgr
    base = f"http://127.0.0.1:{harness.r.config.http_port}/api/autostart"
    async with aiohttp.ClientSession() as s:
        async with s.get(base) as resp:
            ov = await resp.json()
            assert resp.status == 200 and ov["supported"] and ov["services"] == []
        async with s.get(f"{base}/browse", params={"path": str(env.launch.parent)}) as resp:
            b = await resp.json()
            assert resp.status == 200 and b["entries"][0]["launch"] is True
        async with s.get(f"{base}/browse", params={"path": str(env.tmp)}) as resp:
            assert resp.status == 403
        async with s.put(f"{base}/robot", json={"launch": {"file": str(env.launch)}, "args": ["x:=1\n"]}) as resp:
            body = await resp.json()
            assert resp.status == 400 and body["errors"] and "one line" in body["errors"][0]
        async with s.put(f"{base}/Bad_Name", json={"launch": {"file": str(env.launch)}}) as resp:
            assert resp.status == 400
        async with s.put(f"{base}/robot", json={"launch": {"file": str(env.launch)}, "start_now": True}) as resp:
            svc = await resp.json()
            assert resp.status == 200 and svc["name"] == "robot" and svc["active"] == "active"
        async with s.post(f"{base}/robot/stop") as resp:
            assert resp.status == 200 and (await resp.json())["active"] == "inactive"
        async with s.post(f"{base}/robot/start") as resp:
            assert resp.status == 200 and (await resp.json())["active"] == "active"
        async with s.post(f"{base}/ghost/start") as resp:
            assert resp.status == 404
        async with s.get(f"{base}/robot/log", params={"lines": "2"}) as resp:
            assert resp.status == 200 and len((await resp.json())["lines"]) == 2
        async with s.post(f"{base}/linger") as resp:
            assert (await resp.json()) == {"linger": False, "command": "sudo loginctl enable-linger pi"}
        async with s.delete(f"{base}/robot") as resp:
            assert resp.status == 200 and (await resp.json()) == {"removed": "robot", "self": False}
        assert not mgr.paths("robot")[0].exists()
        # the same API through the /mission/api tunnel
        status, ov = await harness.r.call_api("GET", "/api/autostart")
        assert status == 200 and ov["services"] == []
    changed = [e["name"] for e in harness.events if e["type"] == "autostart.changed"]
    assert changed == ["robot", "robot", "robot", "robot"]


async def test_http_autostart_disabled_and_unsupported(harness, env):
    import aiohttp

    harness.r.autostart = env.manager(config={"enabled": False, "roots": ["~"]})
    base = f"http://127.0.0.1:{harness.r.config.http_port}/api/autostart"
    async with aiohttp.ClientSession() as s:
        async with s.get(base) as resp:
            assert resp.status == 200 and (await resp.json())["enabled"] is False
        for method, url in [("PUT", f"{base}/robot"), ("GET", f"{base}/browse"), ("POST", f"{base}/robot/start"), ("DELETE", f"{base}/robot"), ("GET", f"{base}/robot/log"), ("POST", f"{base}/linger")]:
            async with s.request(method, url, json={"launch": {"file": str(env.launch)}} if method == "PUT" else None) as resp:
                assert resp.status == 403, (method, url)
        harness.r.autostart = env.manager(platform="darwin")
        async with s.get(base) as resp:
            ov = await resp.json()
            assert ov["supported"] is False and ov["reason"]
        async with s.put(f"{base}/robot", json={"launch": {"file": str(env.launch)}}) as resp:
            assert resp.status == 409
    # only the read-only probes of the overview reached "systemd"
    assert all(c[:3] == ["systemctl", "--user", "show-environment"] or c[:2] == ["loginctl", "show-user"] for c in env.fake.calls)


async def test_http_stopping_or_removing_the_runners_own_service_answers_first(harness, env):
    import aiohttp

    env.cgroup.write_text("0::/user.slice/user-1000.slice/user@1000.service/app.slice/mission-autostart-mission.service\n")
    mgr = env.manager(environ={"INVOCATION_ID": "abc"})
    harness.r.autostart = mgr
    mgr.put("mission", {"launch": {"package": "mission_runner", "file": "bringup.launch.py"}, "start_now": True})
    env.fake.calls.clear()
    base = f"http://127.0.0.1:{harness.r.config.http_port}/api/autostart"
    async with aiohttp.ClientSession() as s:
        async with s.get(base) as resp:
            assert (await resp.json())["self"] == "mission"
        async with s.post(f"{base}/mission/stop") as resp:
            svc = await resp.json()
            assert resp.status == 200 and svc["self"] is True and svc["active"] == "active"
        assert "stop mission-autostart-mission.service" not in env.fake.verbs()
        await _wait_for(lambda: "--no-block stop mission-autostart-mission.service" in env.fake.verbs())
        env.fake.calls.clear()
        async with s.delete(f"{base}/mission") as resp:
            assert resp.status == 200 and (await resp.json()) == {"removed": "mission", "self": True}
        assert env.fake.verbs() == ["disable mission-autostart-mission.service", "daemon-reload"]
        assert not mgr.paths("mission")[2].exists()
        await _wait_for(lambda: "--no-block stop mission-autostart-mission.service" in env.fake.verbs())


async def _wait_for(cond, timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    while loop.time() - t0 < timeout:
        if cond():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition not met in time")
