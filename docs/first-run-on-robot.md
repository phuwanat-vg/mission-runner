# First run on a real robot

Everything so far has been tested against the simulated robot. This is the
checklist for the first time the code touches hardware: what to install, what
order to start things in, what to check before each next step, and what usually
goes wrong.

Work through it with the robot **on blocks or in a clear area**, and keep a
hand on the hardware e-stop. The very first `nav.follow_route` will drive.

---

## 0. The short way: one command

On a Pi with Ubuntu 24.04 and ROS 2 Jazzy:

```bash
curl -fsSL https://raw.githubusercontent.com/phuwanat-vg/mission-runner/main/install.sh | bash
```

It asks for sudo once, then: clones the code into `~/ros2_ws/src/mission-runner`
(or pulls it when it is already there), runs `rosdep install`, builds
`mission_msgs` and `mission_runner`, adds `source ~/ros2_ws/install/setup.bash`
to `~/.bashrc`, turns on linger, creates the **`mission` autostart service**
(`bringup.launch.py`: mission_runner + foxglove_bridge) and prints the address
to type into Mission Builder:

```
In Mission Builder, connect to one of:
    ws://192.168.1.42:8765        (web page: http://192.168.1.42:8080)
```

Options go after `bash -s --`: `--ws ~/robot_ws`, `--domain 7` (ROS_DOMAIN_ID),
`--project ~/line2.mproj` (imported each time the service starts),
`--no-autostart`. Run the same command again to update: it pulls, rebuilds and
restarts the service, keeping its arguments.

Then continue with section 2 (configure), add your robot's own launch file as
a service (section 3), and go through section 5. Sections 1 and 3 below are
the same steps by hand.

---

## 1. What the robot needs

On the Pi (ROS 2 Jazzy):

```bash
cd ~/ros2_ws/src
git clone https://github.com/phuwanat-vg/mission-runner.git
cd ~/ros2_ws
rosdep install --from-paths src -y --ignore-src   # nav2_simple_commander, foxglove_bridge, python3-aiohttp, ...
colcon build --packages-select mission_msgs mission_runner
source install/setup.bash
```

Optional, only if a mission uses them: `pip install pymodbus`,
`sudo apt install python3-gpiozero` (also needed by `gpio` station answer nodes).
For map files you also want `ros-$ROS_DISTRO-nav2-map-server`.

`mission_msgs` is what makes `/mission/api` exist. Without it, Mission Builder connects and
draws the map but cannot load, deploy or run missions, and says so.

---

## 2. Configure the runner

```bash
mkdir -p ~/.mission
cp ~/ros2_ws/src/mission-runner/runner/config/runner.example.yaml ~/.mission/runner.yaml
```

The two settings that matter on day one:

```yaml
nav2:
  wait_nodes: [bt_navigator]   # what nav.wait_active waits for
  localizer: amcl              # "" if you localise with slam_toolbox or FAST-LIO2
robot_frame: base_link         # the frame that TF says the robot is at
```

`nav.wait_active` waits for those lifecycle nodes to report ACTIVE. If you do
not run AMCL, set `localizer: ""` or the first step of every mission hangs.

With AMCL, pick a Home point as the map's initial pose instead of AMCL's
`set_initial_pose`; mission_runner then localizes the robot there at boot (see
[robot-startup.md](robot-startup.md#home-as-initial-pose)).

Connectors (MQTT, Modbus) are optional; skip `connectors.yaml` for the first
run. A mission that references a connector you have not configured still loads
— the action just fails when it runs.

---

## 3. Start in this order, checking each one

The robot runs two layers, so Nav2 keeps running when the mission layer
restarts:

| Layer | What | How it starts at boot |
|---|---|---|
| `robot` | your own launch file: drivers, localization, Nav2 | autostart service `robot` |
| `mission` | `bringup.launch.py`: mission_runner + foxglove_bridge + station answer nodes | autostart service `mission`, after `robot` |

**a. Nav2 and localisation, the way you normally do.** Confirm it works on its
own before adding anything:

```bash
ros2 launch my_robot robot.launch.py
ros2 topic echo /map --once            # a map is published
ros2 run tf2_ros tf2_echo map base_link   # the robot has a pose in the map
ros2 lifecycle get /bt_navigator       # active
```

If any of those fail, stop here. Nothing below can work until they do.

**b. The mission layer**

```bash
ros2 launch mission_runner bringup.launch.py
```

That is mission_runner plus `foxglove_bridge` on port 8765 with
`include_hidden:=true` (iViz needs the hidden action topics). Useful arguments:
`project:=~/line2.mproj` imports a project first (an invalid project stops
the runner with the errors printed), `stations:=~/.mission/stations.yaml`
starts a `station_answer` node per station (see
`runner/config/stations.example.yaml`), `bridge:=false` if you run the bridge
yourself.

The manual path still works:

```bash
ros2 launch foxglove_bridge foxglove_bridge_launch.xml port:=8765 include_hidden:=true
ros2 launch mission_runner mission_runner.launch.py
```

Watch the log. A healthy start ends with:

```
mission_runner 0.1.0 ready: backend=nav2 home=/home/pi/.mission http=0.0.0.0:8080 missions=N
```

Then check the ROS side is up:

```bash
ros2 service list | grep mission     # /mission/api and /missions/<name>/run
ros2 topic echo /mission/state --once
```

**c. Mission Builder on Windows.** Enter `ws://<pi-ip>:8765` and press Connect.
If it cannot load missions, its message says whether the bridge has no
`services` capability or `/mission/api` is missing.

**d. Make both start at boot.** Once a and b work by hand, stop them (Ctrl+C)
and turn them into services, in Mission Builder (**... menu → Robot startup**)
or on the Pi:

```bash
sudo loginctl enable-linger $USER    # once, so user services start without a login
mission_runner autostart add robot --launch ~/robot_ws/src/my_robot/launch/robot.launch.py --start
mission_runner autostart add mission --package mission_runner --launch bringup.launch.py --after robot --start
mission_runner autostart list
mission_runner autostart log mission          # the journal of one service
```

No root is needed to add, change or remove them. `mission_runner` still waits
for Nav2 to become active, so the order at boot is safe even when Nav2 takes a
minute. `mission_runner autostart remove mission` takes a service away again.

---

## 3b. Putting a project on the robot without a network

A project saved by Mission Builder (`.mproj`) can be copied to the robot on a
USB stick or with `scp`, then imported with no GUI and no bridge:

```bash
mission_runner project import ~/line3.mproj           # add or update
mission_runner project import ~/line3.mproj --replace # also remove missions not in the project
mission_runner autostart restart mission
```

Everything is validated first; if any mission is invalid nothing is written
and the errors are printed. `mission_runner project export robot.mproj` goes
the other way. To import it every time the mission layer starts instead, give
the service the argument:
`mission_runner autostart add mission --arg project:=/home/pi/line3.mproj --start`.

---

## 4. Tell the system about your map

Mission Builder draws points on the map the robot is using. Register it once in
the **Maps** tab, or by editing `~/.mission/sites.json`:

```json
{
  "schema": "sites/1",
  "default_map": "factory",
  "maps": {
    "factory": {
      "file": "/home/pi/maps/factory.yaml",
      "frame": "map",
      "sites": {}
    }
  }
}
```

`file` is the map yaml `map_server` loads. It is what `nav.change_map` switches
to, and what Mission Builder shows as the floor. If the path is wrong you get a plain
grey room instead of your floor plan, and everything else still works.

---

## 5. Prove the chain end to end, smallest first

**Step 1 — can the runner talk to Nav2 at all?** From the Pi, with the robot
somewhere safe:

```bash
ros2 service call /missions/go_to_point/run std_srvs/srv/Trigger
```

`go_to_point` is one of the bundled examples and drives to (1.0, 0.5). Edit it
first if that is not a safe spot: open it in Mission Builder, or edit
`~/.mission/missions/go_to_point.json`. Watch `/mission/state` and the Nav2
logs. **This is the moment the untested code meets real hardware** — expect to
find something here rather than later.

**Step 2 — does Mission Builder drive it?** Select the same mission in the tree
and press **Run**. Same behaviour, now from Windows.

**Step 3 — draw a route.** In Mission Builder: press `N`, click two or three safe
spots, name them, press `L`, drag between them to connect. Press **Deploy**,
which saves the map data too. Confirm `~/.mission/sites.json` has your points and edges.

**Step 4 — a mission that follows the graph.** Open a mission, add a *Drive somewhere → Follow route*
task for each point in order, then Run. The robot should drive along the lanes
you drew, not straight across. Watch each task tick off in the tree.

**Step 5 — the safety paths.** While it is driving, press **Stop**. It should
cancel the Nav2 goal within a second or so. Then try the hardware e-stop, and
check the run ends and the `on_abort` steps run.

---

## 6. When something goes wrong

| What you see | Usually means |
|---|---|
| Mission Builder cannot load missions | `mission_msgs` is not built, or the bridge does not advertise services. `ros2 service list \| grep mission/api` |
| Mission Builder shows no missions | The runner is not running, or it started before your workspace was sourced |
| A service shows *Failed, restarted N times* | `mission_runner autostart log <name>`: usually a workspace that is not built, or a launch argument the file does not declare |
| Services run after login but not after a reboot | linger is off: `sudo loginctl enable-linger $USER` |
| Every mission hangs on the first step | `nav.wait_active` is waiting for a lifecycle node that never becomes active. Fix `nav2.wait_nodes` / `localizer` in `runner.yaml` |
| "robot pose unknown" | TF has no `map` → `base_link`. Check `robot_frame`, and that localisation is running |
| "site X not found in map Y" | The mission names a site that is not in the current map. Check `default_map` and the site names |
| "no route from A to B" | The two points are not connected in the graph, or a lane is blocked or one-way. Draw the missing lane, or set `on_no_route: direct` on that step to fall back to driving straight there |
| Goal rejected / fails immediately | Nav2 refused it: goal in an obstacle, outside the map, or a costmap not yet ready. The step's error carries Nav2's `error_code` |
| Floor plan is a plain grey room | The map `file` path in `sites.json` does not exist on the robot |
| Docking steps fail | `nav2_simple_commander` on your distro has no docking API, or no docking server is running. `GET /api/capabilities` reports which nav steps are available |
| A `ros.request` is never answered | Nobody listens on its request topic: `ros2 topic info /station/<slug>/request`. Start a `station_answer` node (`stations:=` in `bringup.launch.py`) |

Logs worth having open:

```bash
mission_runner autostart log mission --lines 100
ros2 topic echo /mission/event      # every step start/finish, as JSON
curl http://<pi-ip>:8080/api/runs   # the run history with per-step results
```

The runner also serves a small page at `http://<pi-ip>:8080` from a phone:
STOP, run buttons and prompt answers. Useful as a second pair of hands while
you are next to the robot.

---

## 7. Before you leave it running unattended

- Make both layers autostart services (section 3d) and turn on linger, so the
  robot comes back by itself after a power cut. (The older system unit
  `runner/deploy/mission_runner.service` still works if you prefer a root-managed
  service; do not use both.)
- Give risky missions an `on_abort` that leaves the robot and the line in a
  safe state.
- Add an interrupt for low battery (`examples/go_charge.json` shows the shape)
  and bind the e-stop input in `examples/global.json` to your GPIO pin.
- Cap speed where people walk: draw a `speed_limit` zone or set `speed_mps` on
  the lanes, and export the Nav2 filter masks with
  `POST /api/maps/<map>/filters`.
- Give every `ask_user` step a `timeout_s` and a `default`, or an unattended
  robot waits forever for an answer nobody will give.

---

## Known state of the code

The interpreter, dispatcher, route planning, triggers, policies and the HTTP
and ROS interfaces are covered by automated tests, all against the simulated
robot. `bringup.launch.py`, `station_answer`, the example nodes and autostart
services with systemd have run in a ROS 2 Jazzy install (WSL, no Nav2 running).
`backends/nav2.py` — the part that drives `nav2_simple_commander` — has not yet
run against a real Nav2. Treat step 5 above as its first test, and expect to
file a fix or two.
