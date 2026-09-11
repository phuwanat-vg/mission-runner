"""Command line: ``mission_runner [run] [--sim] [--home DIR] [--port N]``,
``mission_runner validate FILE...``, ``mission_runner examples``,
``mission_runner bt '{"template": ...}'``."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import sys
from pathlib import Path

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
    run.add_argument("ros_args", nargs="*", help=argparse.SUPPRESS)

    val = sub.add_parser("validate", help="validate mission files")
    _add_common(val)
    val.add_argument("files", nargs="+")
    val.add_argument("--sites", help="sites.json to check site names against")

    ex = sub.add_parser("examples", help="copy the example missions into the home directory")
    _add_common(ex)

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

    from .runner import Runner

    if args.cmd == "examples":
        r = Runner(cfg)
        names = r.install_examples()
        print(f"installed {len(names)} missions into {cfg.home / 'missions'}: {', '.join(names)}")
        return 0

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
