# mission_runner

ROS 2 package (ament_python) that executes `mission/1` JSON missions against
Nav2 and exposes them over HTTP/WebSocket, ROS services/actions, MQTT and
friends. See the repository [README](../README.md) for the big picture and
[docs/runner-api.md](../docs/runner-api.md) for the interfaces.

```
mission_runner/
  cli.py            mission_runner run | validate | examples | bt
  runner.py         composition root (store, backend, connectors, dispatcher, server)
  interpreter.py    executes a flow: steps, retries, timeouts, pause/suspend/resume
  dispatcher.py     one active run, priority queue, policies, interrupts
  triggers.py       arms event sources with when/edge/debounce, wait_event
  prompts.py        ask_user
  model.py          JSON Schema + semantic validation, Mission/Step/Trigger, sites
  expressions.py    safe expression evaluator ($name, ${expr})
  bt.py             behavior-tree XML templates
  store.py          missions/sites/connectors on disk, SQLite run log
  server.py         aiohttp API, WebSocket events, web page
  backends/         sim.py (no ROS)  nav2.py (BasicNavigator)
  connectors/       timer, internal (webhook, mission.done), mqtt, modbus, gpio, ros
  schema/           mission.schema.json, sites.schema.json
  webui/            built editor (npm run build in ../editor)
```

Run without ROS: `python -m mission_runner run --sim`.
Tests: `python -m pytest` (uses the sim backend, no network).
