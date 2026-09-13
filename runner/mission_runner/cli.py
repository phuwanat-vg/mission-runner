"""Command line: ``mission_runner [run] [--sim] [--home DIR] [--port N] [--project FILE]``,
``mission_runner validate FILE...``, ``mission_runner examples``,
``mission_runner project import|export FILE``, ``mission_runner autostart ...``,
``mission_runner bt '{"template": ...}'``."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
from pathlib import Path
from typing import Any

from . import __version__


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--home", help="data directory (default: $MISSION_HOME or ~/.mission)")
    p.add_argument("--log-level", default=None, help="DEBUG, INFO, WARNING, ERROR")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mission_runner", description="Run declarative Nav2 missions.")
    parser.add_argument("--version", action="version", version=f"mission_runner {__version__}")
    sub = parser.add_subparsers(dest="cmd")

    run = sub.add_parser("run", help="start the runner (default)")
    _add_common(run)
    run.add_argument("--sim", action="store_true", help="simulated robot, no ROS needed")
    run.add_argument("--host", default=None, help="HTTP bind address (default 0.0.0.0)")
    run.add_argument("--port", type=int, default=None, help="HTTP port (default 8080)")
    run.add_argument("--time-scale", type=float, default=None, help="sim: speed up simulated time")
    run.add_argument("--no-examples", action="store_true", help="do not install example missions into an empty home")
    run.add_argument("--project", default=None, help="import this project file (.mproj) before starting; an invalid project stops here")
    run.add_argument("--project-replace", action="store_true", help="with --project: also delete missions that are not in the project")
    run.add_argument("ros_args", nargs="*", help=argparse.SUPPRESS)

    val = sub.add_parser("validate", help="validate mission files")
    _add_common(val)
    val.add_argument("files", nargs="+")
    val.add_argument("--sites", help="sites.json to check site names against")

    ex = sub.add_parser("examples", help="copy the example missions into the home directory")
    _add_common(ex)

    proj = sub.add_parser("project", help="import or export a project file (.mproj); no network or GUI needed")
    _add_common(proj)
    proj.add_argument("action", choices=["import", "export"])
    proj.add_argument("file")
    proj.add_argument("--replace", action="store_true", help="import: also delete missions that are not in the project")
    proj.add_argument("--name", default=None, help="export: project name (default: the home directory's name)")

    auto = sub.add_parser(
        "autostart",
        help="services that launch the robot and the mission layer at boot (systemd user units)",
        description="Examples:\n"
        "  mission_runner autostart add robot --launch ~/robot_ws/src/my_robot/launch/robot.launch.py --arg use_sim_time:=false\n"
        "  mission_runner autostart add mission --package mission_runner --launch bringup.launch.py --after robot --start\n"
        "  mission_runner autostart list | start NAME | stop NAME | restart NAME | log NAME | remove NAME | linger\n"
        "`add` on an existing service changes only the options given.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_common(auto)
    auto.add_argument("action", choices=["list", "add", "start", "stop", "restart", "remove", "log", "linger"])
    auto.add_argument("name", nargs="?")
    auto.add_argument("--launch", help="add: launch file path, or the file name inside --package")
    auto.add_argument("--package", help="add: ROS package that contains the launch file")
    auto.add_argument("--arg", action="append", dest="args", metavar="NAME:=VALUE", help="add: launch argument (repeatable); replaces an existing argument of the same name")
    auto.add_argument("--clear-args", action="store_true", help="add: drop the existing launch arguments first")
    auto.add_argument("--workspace", action="append", dest="workspaces", metavar="SETUP_BASH", help="add: setup.bash to source, in order (default: detected)")
    auto.add_argument("--description", help="add: one-line description")
    auto.add_argument("--domain", type=int, default=None, metavar="N", help="add: ROS_DOMAIN_ID")
    auto.add_argument("--rmw", help="add: RMW_IMPLEMENTATION, e.g. rmw_cyclonedds_cpp")
    auto.add_argument("--after", action="append", metavar="NAME", help="add: start after this service (repeatable)")
    auto.add_argument("--start", action="store_true", help="add: (re)start the service now")
    auto.add_argument("--lines", type=int, default=200, help="log: number of lines")
    auto.add_argument("--json", action="store_true", help="list: print JSON")

    bt = sub.add_parser("bt", help="print the behavior tree XML for a template JSON object")
    bt.add_argument("template", help='e.g. \'{"template": "navigate_with_recovery", "retries": 4}\'')
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # ros2 launch passes "--ros-args ..."; strip them before argparse sees them.
    if "--ros-args" in argv:
        argv = argv[: argv.index("--ros-args")]
    if not argv or argv[0].startswith("-"):
        argv = ["run", *argv]
    args = build_parser().parse_args(argv)

    if args.cmd == "bt":
        from .bt import render_behavior_tree

        print(render_behavior_tree(json.loads(args.template)), end="")
        return 0

    from .config import RunnerConfig

    if args.cmd == "validate":
        return _validate(args)

    cfg = RunnerConfig.load(
        args.home,
        backend="sim" if getattr(args, "sim", False) else None,
        http_host=getattr(args, "host", None),
        http_port=getattr(args, "port", None),
        log_level=args.log_level,
        install_examples=False if getattr(args, "no_examples", False) else None,
    )
    if getattr(args, "time_scale", None):
        cfg.sim = {**cfg.sim, "time_scale": args.time_scale}
    logging.basicConfig(level=getattr(logging, str(cfg.log_level).upper(), logging.INFO), format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("aiohttp").setLevel(logging.WARNING)

    if args.cmd == "project":
        return _project(args, cfg)
    if args.cmd == "autostart":
        return _autostart(args, cfg)

    from .runner import Runner

    if args.cmd == "examples":
        r = Runner(cfg)
        names = r.install_examples()
        print(f"installed {len(names)} missions into {cfg.home / 'missions'}: {', '.join(names)}")
        return 0

    if getattr(args, "project", None) and args.project.strip():
        rc = _import_project(Path(os.path.expanduser(args.project.strip())), cfg, args.project_replace, hint=False)
        if rc != 0:
            print("mission_runner not started: fix the project file first.")
            return rc

    return asyncio.run(_serve(cfg))


async def _serve(cfg: object) -> int:
    from .runner import Runner

    runner = Runner(cfg)  # type: ignore[arg-type]
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):  # Windows
            signal.signal(sig, lambda *_: stop.set())
    try:
        await runner.start()
    except Exception as e:  # noqa: BLE001
        logging.getLogger("mission").error("startup failed: %s", e, exc_info=True)
        await runner.stop()
        return 1
    try:
        await stop.wait()
    finally:
        await runner.stop()
    return 0


def _project(args: argparse.Namespace, cfg: object) -> int:
    from .project import export_project
    from .store import MissionStore

    home = cfg.home  # type: ignore[attr-defined]
    path = Path(args.file)

    if args.action == "export":
        store = MissionStore(home)
        store.load_all()
        store.load_sites()
        doc = export_project(
            store,
            args.name or home.name,
            {"request_topic": cfg.request_topic, "answer_topic": cfg.answer_topic},  # type: ignore[attr-defined]
        )
        path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"exported {len(doc['missions'])} missions and {len(doc['sites'].get('maps', {}))} maps to {path}")
        return 0
    return _import_project(path, cfg, args.replace, hint=True)


def _import_project(path: Path, cfg: object, replace: bool, hint: bool) -> int:
    """Validate a project file and write it into the home. Nothing is written when
    anything is invalid. 0 = imported, 1 = invalid, 2 = unreadable."""
    from .project import check_project, write_project
    from .store import MissionStore

    home = cfg.home  # type: ignore[attr-defined]
    try:
        doc = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError) as e:
        print(f"{path}: {e}")
        return 2
    errors, warnings, book, _missions = check_project(doc)
    for w in warnings:
        print(f"  warning {w}")
    if errors or book is None:
        for e in errors:
            print(f"  error   {e}")
        print(f"{path}: not imported, {len(errors)} errors. Nothing was written.")
        return 1
    store = MissionStore(home)
    store.load_all()
    store.load_sites()
    result = write_project(store, doc, book, replace=replace)
    deleted = f"; deleted {', '.join(result['deleted'])}" if result["deleted"] else ""
    print(f"imported {path} into {home}: saved {', '.join(result['saved']) or 'nothing'}{deleted}")
    if hint:
        print("A running mission_runner picks this up after a restart: mission_runner autostart restart mission (or sudo systemctl restart mission_runner)")
    return 0


def _autostart(args: argparse.Namespace, cfg: object) -> int:
    """The CLI works whatever ``autostart.enabled`` says: it runs as the robot's user."""
    from .autostart import AutostartError, AutostartManager

    mgr = AutostartManager(cfg.home, cfg.autostart)  # type: ignore[attr-defined]
    try:
        return _autostart_cmd(mgr, args)
    except AutostartError as e:
        for msg in e.errors:
            print(f"  error   {msg}")
        return 1


def _print_service(s: dict) -> None:
    launch = s["launch"]
    target = f"{launch.get('package')} {launch.get('file')}" if launch.get("package") else str(launch.get("file", ""))
    state = s["active"] + (f" ({s['sub_state']})" if s["sub_state"] and s["sub_state"] != s["active"] else "")
    extra = []
    if s["since"]:
        extra.append(f"since {s['since']}")
    if s["restarts"]:
        extra.append(f"restarted {s['restarts']} times")
    if s["main_pid"]:
        extra.append(f"pid {s['main_pid']}")
    print(f"{s['name']:<16} {state:<22} {'enabled' if s['enabled'] else 'disabled':<9} {', '.join(extra)}")
    print(f"{'':<16} ros2 launch {target} {' '.join(s['args'])}".rstrip())
    if s["after"]:
        print(f"{'':<16} starts after {', '.join(s['after'])}")
    if s.get("ros_domain_id") is not None or s.get("rmw"):
        print(f"{'':<16} ROS_DOMAIN_ID={s.get('ros_domain_id')} RMW={s.get('rmw') or 'default'}")


def _autostart_cmd(mgr: Any, args: argparse.Namespace) -> int:
    from .autostart import AutostartError

    action, name = args.action, args.name
    if action == "list":
        ov = mgr.overview()
        if args.json:
            print(json.dumps(ov, indent=2))
            return 0
        if not ov["supported"]:
            print(f"autostart is not supported here: {ov['reason']}")
        else:
            print(f"user {ov['user']}, ROS {ov['ros_distro'] or '?'}, linger {'on' if ov['linger'] else 'OFF: services start at boot only after: sudo loginctl enable-linger ' + ov['user']}")
        if not ov["services"]:
            print("no autostart services")
        for s in ov["services"]:
            _print_service(s)
        return 0
    if action == "linger":
        out = mgr.enable_linger()
        print("linger is on: services start at boot without a login" if out["linger"] else f"not allowed without root; run once: {out['command']}")
        return 0 if out["linger"] else 1
    if not name:
        raise AutostartError(400, f"autostart {action} needs a service name")

    if action == "add":
        body: dict = {}
        existing = name in mgr.names()
        if existing:
            old = mgr.service(name, supported=False)
            body = {k: old[k] for k in ("description", "launch", "args", "workspaces", "ros_domain_id", "rmw", "after")}
        if args.launch:
            if args.package:
                body["launch"] = {"package": args.package, "file": args.launch}
            else:
                body["launch"] = {"file": os.path.abspath(os.path.expanduser(args.launch))}
            if not args.workspaces:
                body["workspaces"] = []  # detect again for the new target
        elif not existing:
            raise AutostartError(400, "add needs --launch FILE, or --package PKG --launch FILE")
        if args.clear_args:
            body["args"] = []
        if args.args:
            # --arg replaces an argument of the same name and keeps the others
            names = {a.split(":=", 1)[0] for a in args.args}
            body["args"] = [a for a in body.get("args", []) if a.split(":=", 1)[0] not in names] + list(args.args)
        for key, value in (("workspaces", args.workspaces), ("description", args.description), ("ros_domain_id", args.domain), ("rmw", args.rmw), ("after", args.after)):
            if value is not None:
                body[key] = value
        body["start_now"] = bool(args.start)
        s = mgr.put(name, body)
        print(f"{'updated' if existing else 'added'} {s['unit']}{' and (re)started it' if args.start else ''}")
        _print_service(s)
        if not mgr.linger():
            print(f"note: it starts at boot only after linger is on once: sudo loginctl enable-linger {mgr.user}")
        return 0
    if action in ("start", "stop", "restart"):
        _print_service(mgr.action(name, action))
        return 0
    if action == "remove":
        mgr.remove(name)
        print(f"removed {mgr.unit_name(name)}")
        return 0
    if action == "log":
        for line in mgr.log(name, args.lines)["lines"]:
            print(line)
        return 0
    return 2


def _validate(args: argparse.Namespace) -> int:
    from .model import SitesBook, validate_mission

    sites = None
    if args.sites:
        sites = SitesBook.from_dict(json.loads(Path(args.sites).read_text("utf-8")))
    docs = {}
    for f in args.files:
        try:
            docs[f] = json.loads(Path(f).read_text("utf-8"))
        except (OSError, ValueError) as e:
            print(f"{f}: {e}")
            return 2
    names = {d.get("name") for d in docs.values() if isinstance(d, dict)}
    rc = 0
    for f, doc in docs.items():
        errors, warnings, _m = validate_mission(doc, sites=sites, missions=names)
        print(f"{f}: {'OK' if not errors else 'INVALID'}  ({len(errors)} errors, {len(warnings)} warnings)")
        for e in errors:
            print(f"  error   {e}")
        for w in warnings:
            print(f"  warning {w}")
        if errors:
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
