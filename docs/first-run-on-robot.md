# First run on a real robot

Everything so far has been tested against the simulated robot. This is the
checklist for the first time the code touches hardware: what to install, what
order to start things in, what to check before each next step, and what usually
goes wrong.

Work through it with the robot **on blocks or in a clear area**, and keep a
hand on the hardware e-stop. The very first `nav.follow_route` will drive.

---

## 1. What the robot needs

On the Pi (ROS 2 Jazzy):

```bash
sudo apt install \
  ros-$ROS_DISTRO-nav2-simple-commander \
  ros-$ROS_DISTRO-foxglove-bridge \
  ros-$ROS_DISTRO-nav2-map-server \
  python3-aiohttp python3-jsonschema python3-yaml python3-paho-mqtt python3-croniter
```

Optional, only if a mission uses them: `pip install pymodbus gpiozero`.

Build both packages into your workspace:

```bash
cd ~/ros2_ws/src
git clone https://github.com/phuwanat-vg/mission-runner.git
cd ~/ros2_ws && colcon build --packages-select mission_msgs mission_runner
source install/setup.bash
```

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

Connectors (MQTT, Modbus) are optional; skip `connectors.yaml` for the first
run. A mission that references a connector you have not configured still loads
— the action just fails when it runs.

---

## 3. Start in this order, checking each one

**a. Nav2 and localisation, the way you normally do.** Confirm it works on its
own before adding anything:

```bash
ros2 topic echo /map --once            # a map is published
ros2 run tf2_ros tf2_echo map base_link   # the robot has a pose in the map
ros2 lifecycle get /bt_navigator       # active
```

If any of those fail, stop here. Nothing below can work until they do.

**b. foxglove_bridge**

```bash
ros2 launch foxglove_bridge foxglove_bridge_launch.xml port:=8765
```

**c. mission_runner**

```bash
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

**d. Mission Builder on Windows.** Enter `ws://<pi-ip>:8765` and press Connect.
If it cannot load missions, its message says whether the bridge has no
`services` capability or `/mission/api` is missing.

---

## 3b. Putting a project on the robot without a network

A project saved by Mission Builder (`.mproj`) can be copied to the robot on a
USB stick or with `scp`, then imported with no GUI and no bridge:

```bash
mission_runner project import ~/line3.mproj           # add or update
mission_runner project import ~/line3.mproj --replace # also remove missions not in the project
sudo systemctl restart mission_runner
```

Everything is validated first; if any mission is invalid nothing is written
and the errors are printed. `mission_runner project export robot.mproj` goes
the other way.

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
| Every mission hangs on the first step | `nav.wait_active` is waiting for a lifecycle node that never becomes active. Fix `nav2.wait_nodes` / `localizer` in `runner.yaml` |
| "robot pose unknown" | TF has no `map` → `base_link`. Check `robot_frame`, and that localisation is running |
| "site X not found in map Y" | The mission names a site that is not in the current map. Check `default_map` and the site names |
| "no route from A to B" | The two points are not connected in the graph, or a lane is blocked or one-way. Draw the missing lane, or set `on_no_route: direct` on that step to fall back to driving straight there |
| Goal rejected / fails immediately | Nav2 refused it: goal in an obstacle, outside the map, or a costmap not yet ready. The step's error carries Nav2's `error_code` |
| Floor plan is a plain grey room | The map `file` path in `sites.json` does not exist on the robot |
| Docking steps fail | `nav2_simple_commander` on your distro has no docking API, or no docking server is running. `GET /api/capabilities` reports which nav steps are available |

Logs worth having open:

```bash
ros2 launch mission_runner mission_runner.launch.py --ros-args --log-level debug
ros2 topic echo /mission/event      # every step start/finish, as JSON
curl http://<pi-ip>:8080/api/runs   # the run history with per-step results
```

The runner also serves a small page at `http://<pi-ip>:8080` from a phone:
STOP, run buttons and prompt answers. Useful as a second pair of hands while
you are next to the robot.

---

## 7. Before you leave it running unattended

- Set up the systemd unit (`runner/deploy/mission_runner.service`) so the
  runner starts at boot and restarts if it dies.
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
and ROS interfaces are covered by 65 automated tests, all against the simulated
robot. `backends/nav2.py` and `connectors/ros.py` — the parts that actually
touch rclpy and `nav2_simple_commander` — compile and are written against the
documented API, but have not yet run against a real ROS 2 system. Treat step 5
above as their first test, and expect to file a fix or two.
