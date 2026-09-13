"""Autostart services: systemd **user** units that start a ROS 2 launch file at
boot, managed without root. See docs/robot-startup.md, section 4.

For a service named ``robot`` three files are written:

- ``~/.config/systemd/user/mission-autostart-robot.service``
- ``<home>/autostart/robot.sh``: the wrapper the unit executes
- ``<home>/autostart/robot.json``: what was asked for (the source of truth)

The HTTP API lets anyone on the LAN write an executable script, so every name,
path, argument and value is validated strictly and quoted with ``shlex.quote``
before it reaches the wrapper; no user text reaches a shell unquoted.

Every ``systemctl`` / ``journalctl`` / ``loginctl`` call goes through one
injectable :class:`CommandRunner`, so tests fake systemd entirely.
"""

from __future__ import annotations

import getpass
import json
import os
import re
import shlex
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

__all__ = ["AutostartError", "AutostartManager", "CommandResult", "run_command", "is_launch_file", "parse_show"]

NAME_RE = re.compile(r"[a-z][a-z0-9-]{0,31}")
PKG_RE = re.compile(r"[A-Za-z0-9_.-]+")
ARG_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
RMW_RE = re.compile(r"rmw_[a-z0-9_]+")
DISTRO_RE = re.compile(r"[a-z]+")
RESERVED_NAMES = {"browse", "linger"}
LAUNCH_SUFFIXES = (".launch.py", ".launch.xml", ".launch.yaml")
UNIT_PREFIX = "mission-autostart-"
SHOW_PROPS = "ActiveState,SubState,UnitFileState,ActiveEnterTimestamp,NRestarts,MainPID"
MAX_ARGS = 64
MAX_VALUE = 4096
BROWSE_LIMIT = 2000


@dataclass
class CommandResult:
    rc: int
    out: str = ""
    err: str = ""


CommandRunner = Callable[[list[str], Mapping[str, str], float], CommandResult]


def run_command(argv: list[str], env: Mapping[str, str], timeout: float = 30.0) -> CommandResult:
    """The real command runner: no shell, captured output."""
    try:
        p = subprocess.run(argv, capture_output=True, text=True, env=dict(env), timeout=timeout, check=False)
    except FileNotFoundError:
        return CommandResult(127, "", f"{argv[0]}: command not found")
    except subprocess.TimeoutExpired:
        return CommandResult(124, "", f"{' '.join(argv)}: timed out after {timeout:g} s")
    return CommandResult(p.returncode, p.stdout or "", p.stderr or "")


class AutostartError(Exception):
    """A request that cannot be carried out; ``status`` is the HTTP status."""

    def __init__(self, status: int, errors: list[str] | str):
        self.status = status
        self.errors = [errors] if isinstance(errors, str) else list(errors)
        super().__init__("; ".join(self.errors))


def _has_control(s: str) -> bool:
    return any(ord(c) < 32 or ord(c) == 127 for c in s)


def is_launch_file(path: str | os.PathLike[str]) -> bool:
    """``*.launch.py|xml|yaml``, or ``.py|.xml|.yaml`` inside a ``launch`` directory."""
    p = Path(path)
    name = p.name
    if name.endswith(LAUNCH_SUFFIXES):
        return True
    return p.suffix in (".py", ".xml", ".yaml") and p.parent.name == "launch"


def parse_show(text: str) -> dict[str, str]:
    """``Key=Value`` lines of ``systemctl show``."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def parse_timestamp(value: str) -> str | None:
    """systemd's ``Sat 2026-09-13 08:02:11 +07`` (local time) or ``@1757750531`` as ISO 8601."""
    v = (value or "").strip()
    if not v or v == "n/a" or v == "0":
        return None
    try:
        if v.startswith("@"):
            return datetime.fromtimestamp(float(v[1:]), timezone.utc).isoformat()
        parts = v.split()
        if len(parts) >= 3 and re.fullmatch(r"\d{4}-\d{2}-\d{2}", parts[1]):
            return datetime.strptime(f"{parts[1]} {parts[2]}", "%Y-%m-%d %H:%M:%S").astimezone().isoformat()
    except (ValueError, OverflowError, OSError):
        return None
    return None


def _systemd_quote(s: str) -> str:
    """A path for ``ExecStart=``: ``%`` escaped, quoted when it has spaces or quotes."""
    s = s.replace("%", "%%")
    if any(c in s for c in " \t\"'\\"):
        s = '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return s


def _write_atomic(path: Path, text: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    try:
        os.chmod(tmp, mode)
    except OSError:  # pragma: no cover - Windows
        pass
    os.replace(tmp, path)


class AutostartManager:
    """Adds, lists, starts, stops and removes autostart services."""

    def __init__(
        self,
        home: str | os.PathLike[str],
        config: Mapping[str, Any] | None = None,
        *,
        run: CommandRunner | None = None,
        user_home: str | os.PathLike[str] | None = None,
        environ: Mapping[str, str] | None = None,
        platform: str | None = None,
        ros_root: str | os.PathLike[str] = "/opt/ros",
        cgroup_file: str | os.PathLike[str] = "/proc/self/cgroup",
        user: str | None = None,
    ):
        cfg = dict(config or {})
        self.enabled = bool(cfg.get("enabled", True))
        self.home = Path(home).expanduser()
        self.dir = self.home / "autostart"
        self.user_home = Path(user_home) if user_home is not None else Path.home()
        self.environ: Mapping[str, str] = os.environ if environ is None else environ
        self.platform = platform or sys.platform
        self.ros_root = Path(ros_root)
        self.cgroup_file = Path(cgroup_file)
        self._run = run or run_command
        self.user = user or getpass.getuser()
        raw_roots = cfg.get("roots") or ["~", "/opt/ros"]
        self.root_names = [str(r) for r in raw_roots]
        xdg = self.environ.get("XDG_CONFIG_HOME")
        self.unit_dir = (Path(xdg) if xdg else self.user_home / ".config") / "systemd" / "user"

    # ----- environment ------------------------------------------------------------------

    def _expand(self, p: str) -> str:
        if p == "~" or p.startswith("~/"):
            p = str(self.user_home) + p[1:]
        elif p == "$HOME" or p.startswith("$HOME/"):
            p = str(self.user_home) + p[5:]
        return p

    def roots(self) -> list[str]:
        """Allowed roots, symlinks resolved; roots that do not exist are left out."""
        out: list[str] = []
        for r in self.root_names:
            p = self._expand(r)
            if _has_control(p) or not os.path.isabs(p):
                continue
            real = os.path.realpath(p)
            if os.path.isdir(real) and real not in out:
                out.append(real)
        return out

    def under_roots(self, real: str) -> bool:
        for root in self.roots():
            try:
                if os.path.normcase(os.path.commonpath([real, root])) == os.path.normcase(root):
                    return True
            except ValueError:  # different drives
                continue
        return False

    def cmd_env(self) -> dict[str, str]:
        env = dict(self.environ)
        if not env.get("XDG_RUNTIME_DIR") and hasattr(os, "getuid"):
            env["XDG_RUNTIME_DIR"] = f"/run/user/{os.getuid()}"
        return env

    def _cmd(self, *argv: str, timeout: float = 30.0) -> CommandResult:
        return self._run(list(argv), self.cmd_env(), timeout)

    def _systemctl(self, *args: str, timeout: float = 60.0) -> CommandResult:
        return self._cmd("systemctl", "--user", *args, timeout=timeout)

    def _check(self, r: CommandResult, what: str) -> None:
        if r.rc != 0:
            raise AutostartError(500, f"{what} failed: {(r.err or r.out).strip()[:500]}")

    def supported(self) -> tuple[bool, str]:
        if not self.platform.startswith("linux"):
            return False, f"autostart needs Linux with systemd; this runner runs on {self.platform}"
        r = self._systemctl("show-environment", timeout=10.0)
        if r.rc != 0:
            return False, f"systemctl --user is not available: {(r.err or r.out).strip()[:300]}"
        return True, ""

    def ros_distro(self) -> str:
        d = self.environ.get("ROS_DISTRO", "")
        if d and DISTRO_RE.fullmatch(d):
            return d
        try:
            dirs = [p.name for p in self.ros_root.iterdir() if p.is_dir() and DISTRO_RE.fullmatch(p.name)]
        except OSError:
            return ""
        return dirs[0] if len(dirs) == 1 else ""

    def linger(self) -> bool:
        r = self._cmd("loginctl", "show-user", self.user, "-p", "Linger")
        return r.rc == 0 and parse_show(r.out).get("Linger") == "yes"

    def self_name(self) -> str | None:
        """The autostart service this process runs in, if any (``INVOCATION_ID`` + cgroup)."""
        if not self.environ.get("INVOCATION_ID"):
            return None
        try:
            text = self.cgroup_file.read_text("utf-8")
        except OSError:
            return None
        m = re.search(re.escape(UNIT_PREFIX) + r"([a-z][a-z0-9-]{0,31})\.service", text)
        return m.group(1) if m else None

    def is_self(self, name: str) -> bool:
        return self.self_name() == name

    # ----- files -----------------------------------------------------------------------------

    @staticmethod
    def unit_name(name: str) -> str:
        return f"{UNIT_PREFIX}{name}.service"

    def paths(self, name: str) -> tuple[Path, Path, Path]:
        """(unit, wrapper, spec json)."""
        return self.unit_dir / self.unit_name(name), self.dir / f"{name}.sh", self.dir / f"{name}.json"

    def _check_name(self, name: str) -> None:
        if not isinstance(name, str) or not NAME_RE.fullmatch(name) or name in RESERVED_NAMES:
            raise AutostartError(400, f"invalid service name '{name}': use a-z, 0-9 and '-', starting with a letter, at most 32 characters")

    def _load_spec(self, name: str) -> dict[str, Any]:
        self._check_name(name)
        path = self.paths(name)[2]
        try:
            doc = json.loads(path.read_text("utf-8"))
        except FileNotFoundError:
            raise AutostartError(404, f"no autostart service '{name}'") from None
        except (OSError, ValueError) as e:
            raise AutostartError(500, f"{path}: {e}") from None
        if not isinstance(doc, dict):
            raise AutostartError(500, f"{path}: not an object")
        return doc

    def names(self) -> list[str]:
        if not self.dir.is_dir():
            return []
        return sorted(p.stem for p in self.dir.glob("*.json") if NAME_RE.fullmatch(p.stem))

    # ----- validation ----------------------------------------------------------------------------

    def default_workspaces(self, launch: dict[str, str]) -> list[str]:
        """File target: every ancestor's ``install/setup.bash``, nearest last. Package
        target: the workspaces this runner was started from (``COLCON_PREFIX_PATH``)."""
        out: list[str] = []
        if "package" not in launch:
            for anc in reversed(Path(launch["file"]).parents):
                f = anc / "install" / "setup.bash"
                if f.is_file():
                    out.append(os.path.realpath(f))
        else:
            prefixes = [p for p in self.environ.get("COLCON_PREFIX_PATH", "").split(os.pathsep) if p]
            for p in reversed(prefixes):
                f = Path(p) / "setup.bash"
                if f.is_file():
                    out.append(os.path.realpath(f))
        return list(dict.fromkeys(out))

    def validate(self, name: str, body: Any) -> dict[str, Any]:
        """Check a PUT body. Returns the normalized spec or raises ``AutostartError(400)``."""
        self._check_name(name)
        if not isinstance(body, dict):
            raise AutostartError(400, "body must be a JSON object")
        errors: list[str] = []

        description = body.get("description")
        if description is None or description == "":
            description = name
        if not isinstance(description, str) or _has_control(description) or len(description) > 200:
            errors.append("description must be one line of at most 200 characters")
            description = name

        launch = self._validate_launch(body.get("launch"), errors)

        args: list[str] = []
        raw_args = body.get("args") or []
        if not isinstance(raw_args, list) or len(raw_args) > MAX_ARGS:
            errors.append(f"args must be a list of at most {MAX_ARGS} 'name:=value' strings")
            raw_args = []
        for i, a in enumerate(raw_args):
            if not isinstance(a, str) or ":=" not in a:
                errors.append(f"args[{i}] must be 'name:=value'")
                continue
            k, v = a.split(":=", 1)
            if not ARG_NAME_RE.fullmatch(k):
                errors.append(f"args[{i}]: invalid argument name '{k}'")
            elif "\n" in v or "\r" in v or "\x00" in v or len(v) > MAX_VALUE:
                errors.append(f"args[{i}]: the value of '{k}' must be one line")
            else:
                args.append(f"{k}:={v}")

        workspaces: list[str] = []
        raw_ws = body.get("workspaces")
        if raw_ws is None or raw_ws == []:
            workspaces = self.default_workspaces(launch) if launch else []
        elif not isinstance(raw_ws, list):
            errors.append("workspaces must be a list of setup.bash paths")
        else:
            for i, w in enumerate(raw_ws):
                if not isinstance(w, str) or _has_control(w) or not os.path.isabs(self._expand(w)):
                    errors.append(f"workspaces[{i}] must be an absolute path to a setup.bash file")
                    continue
                real = os.path.realpath(self._expand(w))
                if Path(real).name != "setup.bash" or not os.path.isfile(real):
                    errors.append(f"workspaces[{i}]: {w} is not an existing setup.bash file")
                    continue
                workspaces.append(real)

        domain = body.get("ros_domain_id")
        if domain is not None and (isinstance(domain, bool) or not isinstance(domain, int) or not 0 <= domain <= 232):
            errors.append("ros_domain_id must be an integer from 0 to 232")
            domain = None

        rmw = body.get("rmw") or None
        if rmw is not None and (not isinstance(rmw, str) or not RMW_RE.fullmatch(rmw)):
            errors.append("rmw must look like rmw_cyclonedds_cpp")
            rmw = None

        after: list[str] = []
        raw_after = body.get("after") or []
        if not isinstance(raw_after, list):
            errors.append("after must be a list of service names")
            raw_after = []
        existing = set(self.names())
        for a in raw_after:
            if not isinstance(a, str) or not NAME_RE.fullmatch(a):
                errors.append(f"after: invalid service name '{a}'")
            elif a == name:
                errors.append("after: a service cannot start after itself")
            elif a not in existing:
                errors.append(f"after: no autostart service '{a}'")
            elif a not in after:
                after.append(a)

        start_now = body.get("start_now", False)
        if not isinstance(start_now, bool):
            errors.append("start_now must be true or false")

        distro = self.ros_distro()
        if not distro:
            errors.append(f"no ROS 2 distribution found: set ROS_DISTRO or install one under {self.ros_root}")
        elif not (self.ros_root / distro / "setup.bash").is_file():
            errors.append(f"{self.ros_root / distro / 'setup.bash'} does not exist")

        if errors:
            raise AutostartError(400, errors)
        return {
            "name": name,
            "description": description,
            "launch": launch,
            "args": args,
            "workspaces": workspaces,
            "ros_domain_id": domain,
            "rmw": rmw,
            "after": after,
            "ros_distro": distro,
            "start_now": bool(start_now),
        }

    def _validate_launch(self, launch: Any, errors: list[str]) -> dict[str, str]:
        if not isinstance(launch, dict) or not isinstance(launch.get("file"), str) or not launch.get("file"):
            errors.append("launch must be {file} or {package, file}")
            return {}
        file = launch["file"]
        package = launch.get("package")
        if package not in (None, ""):
            ok = True
            for label, v in (("package", package), ("file", file)):
                if not isinstance(v, str) or not PKG_RE.fullmatch(v) or v.startswith("-") or set(v) == {"."}:
                    errors.append(f"launch.{label} must be a plain name (letters, digits, _ . -)")
                    ok = False
            return {"package": package, "file": file} if ok else {}
        path = self._expand(file)
        if _has_control(path) or not os.path.isabs(path):
            errors.append("launch.file must be an absolute path")
            return {}
        real = os.path.realpath(path)
        if not os.path.isfile(real):
            errors.append(f"launch file {file} does not exist")
            return {}
        if not self.under_roots(real):
            errors.append(f"launch file {file} is outside the allowed roots ({', '.join(self.roots()) or 'none'})")
            return {}
        if not is_launch_file(real):
            errors.append(f"{file} is not a launch file (*.launch.py, *.launch.xml, *.launch.yaml, or a .py/.xml/.yaml file in a 'launch' directory)")
            return {}
        return {"file": real}

    # ----- rendering ---------------------------------------------------------------------------

    def render_wrapper(self, spec: dict[str, Any]) -> str:
        q = shlex.quote
        lines = [
            "#!/bin/bash",
            "# Written by mission_runner autostart. Edit through Mission Builder or `mission_runner autostart`.",
            "set -e",
            f"source {q(str(self.ros_root / spec['ros_distro'] / 'setup.bash'))}",
        ]
        lines += [f"source {q(w)}" for w in spec["workspaces"]]
        if spec.get("ros_domain_id") is not None:
            lines.append(f"export ROS_DOMAIN_ID={int(spec['ros_domain_id'])}")
        if spec.get("rmw"):
            lines.append(f"export RMW_IMPLEMENTATION={q(spec['rmw'])}")
        launch = spec["launch"]
        target = [launch["package"], launch["file"]] if "package" in launch else [launch["file"]]
        lines.append(" ".join(["exec ros2 launch", *(q(t) for t in target), *(q(a) for a in spec["args"])]))
        return "\n".join(lines) + "\n"

    def render_unit(self, spec: dict[str, Any]) -> str:
        after = " ".join(["network-online.target", *(self.unit_name(a) for a in spec["after"])])
        description = spec["description"].replace("%", "%%")
        wrapper = self.paths(spec["name"])[1]
        return (
            "[Unit]\n"
            f"Description={description} (mission_runner autostart)\n"
            f"After={after}\n"
            f"Wants={after}\n"
            "StartLimitIntervalSec=0\n"
            "\n"
            "[Service]\n"
            "Type=simple\n"
            f"ExecStart={_systemd_quote(str(wrapper))}\n"
            "Restart=on-failure\n"
            "RestartSec=5\n"
            "KillSignal=SIGINT\n"
            "TimeoutStopSec=30\n"
            "\n"
            "[Install]\n"
            "WantedBy=default.target\n"
        )

    # ----- state ------------------------------------------------------------------------------------

    def service(self, name: str, spec: dict[str, Any] | None = None, supported: bool | None = None) -> dict[str, Any]:
        spec = spec if spec is not None else self._load_spec(name)
        unit = self.unit_name(name)
        out: dict[str, Any] = {
            "name": name,
            "unit": unit,
            "description": spec.get("description", name),
            "launch": spec.get("launch", {}),
            "args": spec.get("args", []),
            "workspaces": spec.get("workspaces", []),
            "ros_domain_id": spec.get("ros_domain_id"),
            "rmw": spec.get("rmw"),
            "after": spec.get("after", []),
            "enabled": False,
            "active": "unknown",
            "sub_state": "",
            "since": None,
            "restarts": 0,
            "main_pid": None,
            "self": self.is_self(name),
        }
        if supported is None:
            supported = self.supported()[0]
        if not supported:
            return out
        r = self._systemctl("show", unit, "-p", SHOW_PROPS, timeout=10.0)
        if r.rc != 0:
            return out
        props = parse_show(r.out)
        pid = props.get("MainPID", "0")
        restarts = props.get("NRestarts", "0")
        out.update(
            enabled=props.get("UnitFileState", "").startswith("enabled"),
            active=props.get("ActiveState", "unknown") or "unknown",
            sub_state=props.get("SubState", ""),
            since=parse_timestamp(props.get("ActiveEnterTimestamp", "")),
            restarts=int(restarts) if restarts.isdigit() else 0,
            main_pid=int(pid) if pid.isdigit() and int(pid) > 0 else None,
        )
        return out

    def list(self, supported: bool | None = None) -> list[dict[str, Any]]:
        if supported is None:
            supported = self.supported()[0]
        out = []
        for name in self.names():
            try:
                out.append(self.service(name, supported=supported))
            except AutostartError:
                continue
        return out

    def overview(self) -> dict[str, Any]:
        ok, reason = self.supported()
        out: dict[str, Any] = {
            "supported": ok,
            "enabled": self.enabled,
            "user": self.user,
            "linger": self.linger() if ok else False,
            "ros_distro": self.ros_distro(),
            "roots": self.roots(),
        }
        if not ok:
            out["reason"] = reason
        me = self.self_name()
        if me:
            out["self"] = me
        out["services"] = self.list(supported=ok)
        return out

    # ----- changes --------------------------------------------------------------------------------

    def require_supported(self) -> None:
        ok, reason = self.supported()
        if not ok:
            raise AutostartError(409, reason)

    def put(self, name: str, body: Any, *, defer_restart: bool = False) -> dict[str, Any]:
        """Validate, write the three files, ``daemon-reload``, ``enable`` and, with
        ``start_now``, ``restart`` (skipped with ``defer_restart``: the caller restarts later)."""
        self.require_supported()
        spec = self.validate(name, body)
        start_now = spec.pop("start_now")
        unit_path, wrapper_path, spec_path = self.paths(name)
        _write_atomic(wrapper_path, self.render_wrapper(spec), 0o755)
        _write_atomic(unit_path, self.render_unit(spec), 0o644)
        _write_atomic(spec_path, json.dumps(spec, indent=2) + "\n", 0o644)
        self._check(self._systemctl("daemon-reload"), "systemctl --user daemon-reload")
        self._check(self._systemctl("enable", self.unit_name(name)), f"systemctl --user enable {self.unit_name(name)}")
        if start_now and not defer_restart:
            self._check(self._systemctl("restart", self.unit_name(name)), f"systemctl --user restart {self.unit_name(name)}")
        return self.service(name, spec, supported=True)

    def action(self, name: str, verb: str, *, no_block: bool = False) -> dict[str, Any]:
        """``start`` / ``stop`` / ``restart``."""
        if verb not in ("start", "stop", "restart"):
            raise AutostartError(400, f"unknown action '{verb}'")
        self.require_supported()
        spec = self._load_spec(name)
        args = ["--no-block", verb] if no_block else [verb]
        self._check(self._systemctl(*args, self.unit_name(name)), f"systemctl --user {verb} {self.unit_name(name)}")
        return self.service(name, spec, supported=True)

    def check_exists(self, name: str) -> None:
        self._load_spec(name)

    def remove(self, name: str, *, stop: bool = True) -> dict[str, Any]:
        """Stop, disable, delete the three files, ``daemon-reload``. With ``stop=False``
        (the runner's own service) the caller stops the unit afterwards."""
        self.require_supported()
        self._load_spec(name)
        unit = self.unit_name(name)
        if stop:
            self._systemctl("stop", unit)
        self._systemctl("disable", unit)
        for p in self.paths(name):
            try:
                p.unlink()
            except FileNotFoundError:
                pass
        self._check(self._systemctl("daemon-reload"), "systemctl --user daemon-reload")
        return {"removed": name, "self": self.is_self(name)}

    def stop_later(self, name: str) -> CommandResult:
        """Stop a unit whose files may already be gone (the self-removal case)."""
        return self._systemctl("--no-block", "stop", self.unit_name(name))

    def log(self, name: str, lines: int = 200) -> dict[str, Any]:
        self.require_supported()
        self._load_spec(name)
        n = max(1, min(int(lines), 5000))
        r = self._cmd("journalctl", "--user", "-u", self.unit_name(name), "-n", str(n), "--no-pager", "-o", "short-iso")
        if r.rc != 0 and not r.out:
            raise AutostartError(500, f"journalctl failed: {r.err.strip()[:500]}")
        return {"lines": r.out.splitlines()}

    def enable_linger(self) -> dict[str, Any]:
        self.require_supported()
        if self.linger():
            return {"linger": True}
        r = self._cmd("loginctl", "enable-linger", self.user)
        if r.rc == 0 and self.linger():
            return {"linger": True}
        return {"linger": False, "command": f"sudo loginctl enable-linger {shlex.quote(self.user)}"}

    # ----- browsing ---------------------------------------------------------------------------------

    def browse(self, path: str | None = None) -> dict[str, Any]:
        roots = self.roots()
        if not roots:
            raise AutostartError(400, "no allowed roots exist")
        raw = self._expand(path) if path else roots[0]
        if _has_control(raw) or not os.path.isabs(raw):
            raise AutostartError(400, "path must be absolute")
        real = os.path.realpath(raw)
        if not self.under_roots(real):
            raise AutostartError(403, f"{path} is outside the allowed roots ({', '.join(roots)})")
        if not os.path.isdir(real):
            raise AutostartError(400, f"{path} is not a directory")
        parent_path = os.path.dirname(real)
        parent = parent_path if parent_path != real and self.under_roots(parent_path) else None
        dirs: list[dict[str, Any]] = []
        files: list[dict[str, Any]] = []
        try:
            entries = sorted(os.scandir(real), key=lambda e: e.name.lower())
        except OSError as e:
            raise AutostartError(403, f"cannot read {real}: {e.strerror}") from None
        for e in entries:
            if e.name.startswith("."):
                continue
            full = os.path.join(real, e.name)
            try:
                target = os.path.realpath(full)
                is_dir = os.path.isdir(target)
                is_file = os.path.isfile(target)
            except OSError:
                continue
            if (is_dir or is_file) and e.is_symlink() and not self.under_roots(target):
                continue
            if is_dir:
                dirs.append({"name": e.name, "path": full, "kind": "dir", "launch": False})
            elif is_file:
                files.append({"name": e.name, "path": full, "kind": "file", "launch": is_launch_file(full)})
            if len(dirs) + len(files) >= BROWSE_LIMIT:
                break
        return {"path": real, "parent": parent, "roots": roots, "entries": dirs + files}
