#!/usr/bin/env bash
# mission_runner installer and updater for a ROS 2 robot (Ubuntu 24.04 + ROS 2 Jazzy).
#
#   curl -fsSL https://raw.githubusercontent.com/phuwanat-vg/mission-runner/main/install.sh | bash
#   curl -fsSL .../install.sh | bash -s -- --ws ~/robot_ws --domain 7 --project ~/line2.mproj
#
# Safe to run again: it pulls the latest code, rebuilds and restarts the
# `mission` autostart service, keeping the service's existing arguments.
#
# Options:
#   --ws DIR          colcon workspace (default ~/ros2_ws)
#   --source DIR      copy the code from this local directory instead of GitHub
#   --branch NAME     git branch (default main)
#   --domain N        ROS_DOMAIN_ID for the mission service
#   --project FILE    project file (.mproj) imported when the mission service starts
#   --no-autostart    do not create or restart the `mission` service
#   --no-sudo         skip everything that needs root (rosdep install, linger)
#   -h, --help

# Everything runs inside main(), called on the last line, so bash has read the
# whole script before any command runs (curl | bash feeds it through stdin).
main() {
  set -euo pipefail
  local REPO_URL="https://github.com/phuwanat-vg/mission-runner.git"
  local WS="$HOME/ros2_ws" SOURCE="" BRANCH="main" DOMAIN="" PROJECT="" AUTOSTART=1 USE_SUDO=1

  while [ $# -gt 0 ]; do
    case "$1" in
      --ws) WS="${2:?--ws needs a directory}"; shift 2 ;;
      --source) SOURCE="${2:?--source needs a directory}"; shift 2 ;;
      --branch) BRANCH="${2:?--branch needs a name}"; shift 2 ;;
      --domain) DOMAIN="${2:?--domain needs a number}"; shift 2 ;;
      --project) PROJECT="${2:?--project needs a file}"; shift 2 ;;
      --no-autostart) AUTOSTART=0; shift ;;
      --no-sudo) USE_SUDO=0; shift ;;
      -h|--help) sed -n '2,19p' "$0" 2>/dev/null || true; return 0 ;;
      *) die "unknown option: $1 (see --help)" ;;
    esac
  done
  if [ -n "$DOMAIN" ] && ! [[ "$DOMAIN" =~ ^[0-9]+$ ]]; then die "--domain must be a number"; fi
  WS="$(abs_path "$WS")"
  if [ -n "$PROJECT" ]; then
    [ -f "$PROJECT" ] || die "project file not found: $PROJECT"
    PROJECT="$(abs_path "$PROJECT")"
  fi
  local SRC="$WS/src/mission-runner"

  # --- ROS 2 -----------------------------------------------------------------
  local DISTRO=""
  if [ -n "${ROS_DISTRO:-}" ] && [ -f "/opt/ros/$ROS_DISTRO/setup.bash" ]; then
    DISTRO="$ROS_DISTRO"
  else
    local found=()
    for d in /opt/ros/*/setup.bash; do [ -f "$d" ] && found+=("$(basename "$(dirname "$d")")"); done
    [ ${#found[@]} -gt 0 ] || die "no ROS 2 installation under /opt/ros. Install ROS 2 Jazzy first: https://docs.ros.org/en/jazzy/Installation.html"
    [ ${#found[@]} -eq 1 ] || die "several ROS 2 distributions (${found[*]}): set ROS_DISTRO, e.g. ROS_DISTRO=jazzy bash install.sh"
    DISTRO="${found[0]}"
  fi
  say "ROS 2 $DISTRO, workspace $WS"
  command -v colcon >/dev/null || die "colcon is missing: sudo apt install python3-colcon-common-extensions"
  command -v rosdep >/dev/null || die "rosdep is missing: sudo apt install python3-rosdep"

  # --- sudo, once ---------------------------------------------------------------
  local SUDO=""
  if [ "$USE_SUDO" = 1 ] && [ "$(id -u)" != 0 ]; then
    command -v sudo >/dev/null || die "sudo is missing (or run with --no-sudo)"
    say "sudo is needed once, for rosdep (apt packages) and to let services start at boot"
    sudo -v </dev/tty 2>/dev/null || sudo -v || die "sudo failed (or run with --no-sudo and do those steps by hand)"
    SUDO="sudo"
  fi

  # --- code ----------------------------------------------------------------------
  mkdir -p "$WS/src"
  if [ -n "$SOURCE" ]; then
    SOURCE="$(abs_path "$SOURCE")"
    [ -f "$SOURCE/runner/package.xml" ] || die "$SOURCE does not look like the mission-runner repository"
    if [ -d "$SRC/.git" ]; then die "$SRC is a git clone; remove it before installing from --source"; fi
    say "copying $SOURCE to $SRC"
    rm -rf "$SRC"
    mkdir -p "$SRC"
    tar -C "$SOURCE" --exclude=.git --exclude=.venv --exclude=node_modules --exclude=__pycache__ --exclude=build --exclude=install --exclude=log -cf - . | tar -C "$SRC" -xf -
  elif [ -d "$SRC/.git" ]; then
    say "updating $SRC"
    git -C "$SRC" pull --ff-only </dev/null
  elif [ -e "$SRC" ]; then
    die "$SRC exists but is not a git clone; move it away or use --source"
  else
    say "cloning $REPO_URL into $SRC"
    git clone --branch "$BRANCH" "$REPO_URL" "$SRC" </dev/null
  fi

  # --- dependencies ------------------------------------------------------------------
  if [ "$USE_SUDO" = 1 ]; then
    if [ ! -f /etc/ros/rosdep/sources.list.d/20-default.list ]; then
      say "rosdep init"
      $SUDO rosdep init </dev/null
    fi
    say "rosdep update"
    rosdep update --rosdistro "$DISTRO" </dev/null >/dev/null
    say "installing dependencies (rosdep install)"
    rosdep install --from-paths "$SRC" -y --ignore-src --rosdistro "$DISTRO" </dev/null
    # Not in rosdep; only cron triggers need it, so a failure is a warning.
    if ! python3 -c "import croniter" 2>/dev/null; then
      say "installing python3-croniter (cron triggers)"
      $SUDO apt-get install -y python3-croniter </dev/null \
        || say "WARNING: python3-croniter could not be installed; cron triggers will not work"
    fi
  else
    say "--no-sudo: skipping rosdep install; missing dependencies:"
    rosdep check --from-paths "$SRC" --ignore-src --rosdistro "$DISTRO" </dev/null || true
  fi

  # --- linger: user services start at boot without a login ---------------------------------
  if loginctl show-user "$USER" -p Linger 2>/dev/null | grep -q "=yes"; then
    say "linger is already on for $USER"
  elif [ -n "$SUDO" ] || [ "$(id -u)" = 0 ]; then
    say "enabling linger for $USER"
    $SUDO loginctl enable-linger "$USER" </dev/null
  elif loginctl enable-linger "$USER" </dev/null 2>/dev/null; then
    say "enabled linger for $USER"
  else
    warn "services start at boot only after this is run once: sudo loginctl enable-linger $USER"
  fi

  # --- build ----------------------------------------------------------------------------------
  say "colcon build --packages-select mission_msgs mission_runner"
  set +u
  # shellcheck disable=SC1090
  source "/opt/ros/$DISTRO/setup.bash"
  set -u
  (cd "$WS" && colcon build --packages-select mission_msgs mission_runner </dev/null)
  set +u
  # shellcheck disable=SC1091
  source "$WS/install/setup.bash"
  set -u

  local LINE="source $WS/install/setup.bash"
  if ! grep -qxF "$LINE" "$HOME/.bashrc" 2>/dev/null; then
    say "adding '$LINE' to ~/.bashrc"
    printf '\n# mission_runner\n%s\n' "$LINE" >>"$HOME/.bashrc"
  fi

  # --- the mission service ----------------------------------------------------------------------
  if [ "$AUTOSTART" = 1 ]; then
    local ARGS=()
    [ -n "$DOMAIN" ] && ARGS+=(--domain "$DOMAIN")
    [ -n "$PROJECT" ] && ARGS+=(--arg "project:=$PROJECT")
    if mission_runner autostart list --json </dev/null | python3 -c 'import json,sys; sys.exit(0 if any(s["name"] == "mission" for s in json.load(sys.stdin)["services"]) else 1)'; then
      say "restarting the mission service (existing arguments kept)"
      mission_runner autostart add mission --workspace "$WS/install/setup.bash" "${ARGS[@]}" --start </dev/null
    else
      say "creating the mission service (mission_runner + foxglove_bridge)"
      mission_runner autostart add mission --package mission_runner --launch bringup.launch.py --description "Mission layer" "${ARGS[@]}" --start </dev/null
    fi
  fi

  # --- summary -----------------------------------------------------------------------------------
  echo
  say "done"
  local ips
  ips="$(hostname -I 2>/dev/null || true)"
  if [ -n "${ips// /}" ]; then
    echo "In Mission Builder, connect to one of:"
    for ip in $ips; do
      case "$ip" in *:*) continue ;; esac
      echo "    ws://$ip:8765        (web page: http://$ip:8080)"
    done
  fi
  echo
  mission_runner autostart list </dev/null || true
}

say() { printf '==> %s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
abs_path() { case "$1" in /*) printf '%s' "$1" ;; "~"*) printf '%s' "$HOME${1#\~}" ;; *) printf '%s' "$PWD/$1" ;; esac; }

main "$@"
