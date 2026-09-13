"""HTTP + WebSocket API (aiohttp) and the web page. See docs/runner-api.md."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aiohttp import WSMsgType, web

from .model import MISSION_SCHEMA, MissionValidationError
from .types import RunSource

if TYPE_CHECKING:
    from .runner import Runner

log = logging.getLogger("mission.http")

WEBUI_DIR = Path(__file__).parent / "webui"


def _json(data: Any, status: int = 200) -> web.Response:
    return web.json_response(data, status=status, dumps=lambda d: json.dumps(d, ensure_ascii=False, default=str))


def _error(message: str, status: int = 400, **extra: Any) -> web.Response:
    return _json({"error": message, **extra}, status)


@web.middleware
async def cors_middleware(request: web.Request, handler: Any) -> web.StreamResponse:
    if request.method == "OPTIONS":
        resp: web.StreamResponse = web.Response(status=204)
    else:
        try:
            resp = await handler(request)
        except web.HTTPException as e:
            resp = e
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp


class HttpServer:
    def __init__(self, runner: Runner):
        self.r = runner
        self.app = web.Application(middlewares=[cors_middleware], client_max_size=4 * 1024 * 1024)
        self._runner_site: web.TCPSite | None = None
        self._app_runner: web.AppRunner | None = None
        self._ws_clients: set[web.WebSocketResponse] = set()
        self._ws_layers: dict[web.WebSocketResponse, set[str]] = {}
        self._unsubscribe = runner.events.subscribe(self._on_event)
        self._routes()

    # ----- lifecycle --------------------------------------------------------------

    async def start(self, host: str, port: int) -> None:
        self._app_runner = web.AppRunner(self.app, access_log=None)
        await self._app_runner.setup()
        # Windows: reuse_address=False so a second runner fails loudly instead of a
        # stale process quietly keeping the port. Linux: SO_REUSEADDR never lets two
        # listeners share a port, and without it a restart (systemctl restart) fails
        # with "address already in use" while old connections sit in TIME_WAIT.
        self._runner_site = web.TCPSite(self._app_runner, host, port, reuse_address=sys.platform != "win32")
        await self._runner_site.start()
        log.info("http on http://%s:%d", host, port)

    async def stop(self) -> None:
        self._unsubscribe()
        for ws in list(self._ws_clients):
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass
        if self._app_runner:
            await self._app_runner.cleanup()

    # ----- routes -------------------------------------------------------------------

    def _routes(self) -> None:
        r = self.app.router
        r.add_get("/api/status", self.get_status)
        r.add_get("/api/capabilities", self.get_capabilities)
        r.add_get("/api/schema", self.get_schema)
        r.add_get("/api/examples", self.get_examples)
        r.add_get("/api/missions", self.get_missions)
        r.add_post("/api/missions/validate", self.post_validate)
        r.add_get("/api/missions/{name}", self.get_mission)
        r.add_put("/api/missions/{name}", self.put_mission)
        r.add_delete("/api/missions/{name}", self.delete_mission)
        r.add_post("/api/missions/{name}/run", self.post_run)
        r.add_get("/api/runs", self.get_runs)
        r.add_get("/api/runs/{id}", self.get_run)
        r.add_post("/api/runs/{id}/cancel", self.post_cancel)
        r.add_post("/api/runs/{id}/pause", self.post_pause)
        r.add_post("/api/runs/{id}/resume", self.post_resume)
        r.add_post("/api/stop", self.post_stop)
        r.add_post("/api/pause", self.post_pause)
        r.add_post("/api/resume", self.post_resume)
        r.add_get("/api/prompt", self.get_prompt)
        r.add_post("/api/prompt/{id}/answer", self.post_answer)
        r.add_get("/api/sites", self.get_sites)
        r.add_put("/api/sites", self.put_sites)
        r.add_get("/api/project", self.get_project)
        r.add_put("/api/project", self.put_project)
        r.add_get("/api/maps/{name}/image", self.get_map_image)
        r.add_post("/api/maps/{name}/filters", self.post_map_filters)
        r.add_get("/api/maps/{name}", self.get_map_meta)
        r.add_post("/api/preview/route", self.post_preview_route)
        r.add_post("/api/preview/dryrun", self.post_preview_dryrun)
        r.add_get("/api/robot/pose", self.get_robot_pose)
        r.add_get("/api/connectors", self.get_connectors)
        r.add_get("/api/autostart", self.get_autostart)
        r.add_get("/api/autostart/browse", self.get_autostart_browse)
        r.add_post("/api/autostart/linger", self.post_autostart_linger)
        r.add_put("/api/autostart/{name}", self.put_autostart)
        r.add_delete("/api/autostart/{name}", self.delete_autostart)
        r.add_post("/api/autostart/{name}/{verb:start|stop|restart}", self.post_autostart_action)
        r.add_get("/api/autostart/{name}/log", self.get_autostart_log)
        r.add_get("/api/events", self.ws_events)
        r.add_post("/hooks/{path:.*}", self.post_hook)
        r.add_post("/api/sim/topic", self.post_sim_topic)
        r.add_post("/api/sim/robot", self.post_sim_robot)
        r.add_get("/", self.index)
        if (WEBUI_DIR / "index.html").exists():
            for sub in ("assets", "icons"):
                if (WEBUI_DIR / sub).is_dir():
                    r.add_static(f"/{sub}", WEBUI_DIR / sub)
            r.add_get("/{file:[^/]+\\.[a-zA-Z0-9]+}", self.webui_file)

    # ----- pages ---------------------------------------------------------------------

    async def index(self, request: web.Request) -> web.StreamResponse:
        idx = WEBUI_DIR / "index.html"
        if idx.exists():
            return web.FileResponse(idx, headers={"Cache-Control": "no-cache"})
        return web.Response(text=FALLBACK_PAGE, content_type="text/html")

    async def webui_file(self, request: web.Request) -> web.StreamResponse:
        name = request.match_info["file"]
        path = (WEBUI_DIR / name).resolve()
        if not str(path).startswith(str(WEBUI_DIR.resolve())) or not path.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(path)

    # ----- status ----------------------------------------------------------------------

    async def get_status(self, request: web.Request) -> web.Response:
        return _json(self.r.status())

    async def get_capabilities(self, request: web.Request) -> web.Response:
        return _json(self.r.capabilities())

    async def get_schema(self, request: web.Request) -> web.Response:
        return _json(MISSION_SCHEMA)

    async def get_examples(self, request: web.Request) -> web.Response:
        return _json(self.r.examples())

    # ----- missions ---------------------------------------------------------------------

    async def get_missions(self, request: web.Request) -> web.Response:
        out = self.r.store.summaries()
        cur = self.r.dispatcher.current
        queued = {q.mission for q in self.r.dispatcher.queue}
        suspended = {s.mission for s, _ in self.r.dispatcher.suspended}
        for m in out:
            m["state"] = "running" if cur and cur.mission == m["name"] else "suspended" if m["name"] in suspended else "queued" if m["name"] in queued else "idle"
            m["trigger_problems"] = self.r.trigger_problems.get(m["name"], [])
        return _json(out)

    async def get_mission(self, request: web.Request) -> web.Response:
        doc = self.r.store.raw(request.match_info["name"])
        if doc is None:
            return _error("no such mission", 404)
        return _json(doc)

    async def _body(self, request: web.Request) -> Any:
        try:
            return await request.json()
        except ValueError:
            raise web.HTTPBadRequest(text=json.dumps({"error": "body must be JSON"}), content_type="application/json") from None

    async def post_validate(self, request: web.Request) -> web.Response:
        doc = await self._body(request)
        errors, warnings, _m = self.r.validate(doc)
        return _json({"ok": not errors, "errors": [e.as_dict() for e in errors], "warnings": [w.as_dict() for w in warnings]})

    async def put_mission(self, request: web.Request) -> web.Response:
        name = request.match_info["name"]
        doc = await self._body(request)
        if not isinstance(doc, dict):
            return _error("mission must be an object")
        if doc.get("name") != name:
            return _error(f"mission name '{doc.get('name')}' does not match the URL '{name}'")
        try:
            m, warnings = await self.r.deploy_mission(doc)
        except MissionValidationError as e:
            return _json({"ok": False, "errors": [x.as_dict() for x in e.errors], "warnings": []}, 400)
        return _json({"ok": True, "name": m.name, "version": m.version, "sha256": m.sha256, "warnings": [w.as_dict() for w in warnings]})

    async def delete_mission(self, request: web.Request) -> web.Response:
        name = request.match_info["name"]
        cur = self.r.dispatcher.current
        if cur and cur.mission == name:
            return _error("mission is running; stop it first", 409)
        ok = await self.r.delete_mission(name)
        return _json({"ok": ok}) if ok else _error("no such mission", 404)

    async def post_run(self, request: web.Request) -> web.Response:
        name = request.match_info["name"]
        body = await self._body(request) if request.can_read_body else {}
        body = body or {}
        accepted, run, reason = await self.r.run_mission(
            name,
            body.get("inputs") or {},
            RunSource("manual", "http", str(body.get("source") or request.remote or "http")),
            body.get("policy"),
            body.get("priority"),
        )
        return _json({"accepted": accepted, "run_id": run.id if run else None, "reason": reason}, 200 if accepted else 409)

    # ----- runs ---------------------------------------------------------------------------

    async def get_runs(self, request: web.Request) -> web.Response:
        limit = int(request.query.get("limit", "50"))
        mission = request.query.get("mission") or None
        return _json(self.r.run_log.list_runs(limit, mission))

    async def get_run(self, request: web.Request) -> web.Response:
        run = self.r.run_log.get_run(request.match_info["id"])
        if run is None:
            live = self.r.dispatcher.find(request.match_info["id"])
            if live is None:
                return _error("no such run", 404)
            return _json({**live.as_dict(), "events": []})
        live = self.r.dispatcher.find(run["id"])
        if live is not None:
            run.update(live.as_dict())
        return _json(run)

    async def post_cancel(self, request: web.Request) -> web.Response:
        ok, msg = await self.r.dispatcher.cancel(request.match_info["id"])
        return _json({"ok": ok, "message": msg}, 200 if ok else 404)

    async def post_pause(self, request: web.Request) -> web.Response:
        ok, msg = await self.r.dispatcher.pause(request.match_info.get("id"))
        return _json({"ok": ok, "message": msg}, 200 if ok else 409)

    async def post_resume(self, request: web.Request) -> web.Response:
        ok, msg = await self.r.dispatcher.resume(request.match_info.get("id"))
        return _json({"ok": ok, "message": msg}, 200 if ok else 409)

    async def post_stop(self, request: web.Request) -> web.Response:
        self.r.events.log("warn", "STOP requested")
        await self.r.backend.cancel()
        asyncio.create_task(self.r.dispatcher.stop_all("stopped by user"))
        return _json({"ok": True})

    # ----- prompts -------------------------------------------------------------------------

    async def get_prompt(self, request: web.Request) -> web.Response:
        p = self.r.prompts.current
        return _json(p.as_dict() if p else None)

    async def post_answer(self, request: web.Request) -> web.Response:
        body = await self._body(request)
        ok, msg = self.r.prompts.answer(request.match_info["id"], str((body or {}).get("answer", "")))
        return _json({"ok": ok, "message": msg}, 200 if ok else 409)

    # ----- sites / robot / connectors ----------------------------------------------------------

    async def get_sites(self, request: web.Request) -> web.Response:
        return _json(self.r.store.sites.to_dict())

    async def put_sites(self, request: web.Request) -> web.Response:
        doc = await self._body(request)
        try:
            book = self.r.store.save_sites(doc)
        except MissionValidationError as e:
            return _json({"ok": False, "errors": [x.as_dict() for x in e.errors]}, 400)
        if self.r.current_map not in book.maps:
            self.r.set_current_map(book.default_map)
        self.r.events.emit("sites.changed")
        return _json({"ok": True, "maps": list(book.maps)})

    def _map_image(self, name: str) -> Any:
        """Cached MapImage for a map name: the real file when it exists, else a
        room drawn around that map's sites."""
        from .mapimage import load_map, synthetic_map

        mapdef = self.r.store.sites.map(name)
        if mapdef is None:
            return None
        key = (name, mapdef.file, tuple(sorted((s.x, s.y) for s in mapdef.sites.values())))
        if self._map_cache_key == key and self._map_cache is not None:
            return self._map_cache
        img = None
        if mapdef.file:
            try:
                img = load_map(mapdef.file)
            except (OSError, ValueError) as e:
                log.info("map '%s' (%s) is not readable, drawing a plain room instead: %s", name, mapdef.file, e)
        if img is None:
            img = synthetic_map([(s.x, s.y) for s in mapdef.sites.values()])
        self._map_cache_key = key
        self._map_cache = img
        return img

    _map_cache: Any = None
    _map_cache_key: Any = None

    async def get_map_meta(self, request: web.Request) -> web.Response:
        name = request.match_info["name"]
        img = self._map_image(name)
        if img is None:
            return _error("no such map", 404)
        mapdef = self.r.store.sites.map(name)
        return _json({**img.meta(), "name": name, "frame": mapdef.frame if mapdef else "map", "file": mapdef.file if mapdef else "", "image_url": f"/api/maps/{name}/image"})

    async def get_map_image(self, request: web.Request) -> web.StreamResponse:
        img = self._map_image(request.match_info["name"])
        if img is None:
            raise web.HTTPNotFound()
        return web.Response(body=img.png, content_type="image/png", headers={"Cache-Control": "no-cache"})

    async def _mission_from_body(self, body: dict[str, Any]) -> Any:
        """Preview endpoints accept a saved mission by name or an unsaved document."""
        from .model import load_mission

        doc = body.get("mission")
        if isinstance(doc, dict):
            return load_mission(doc)
        name = str(body.get("name", ""))
        m = self.r.store.get(name)
        if m is None:
            raise web.HTTPNotFound(text=json.dumps({"error": f"unknown mission '{name}'"}), content_type="application/json")
        return m

    async def post_preview_route(self, request: web.Request) -> web.Response:
        from .preview import plan_route

        body = await self._body(request) or {}
        try:
            mission = await self._mission_from_body(body)
        except MissionValidationError as e:
            return _json({"ok": False, "errors": [x.as_dict() for x in e.errors]}, 400)
        start = body.get("start")
        pose = None
        if isinstance(start, dict):
            from .backends.base import pose_from_any

            pose = pose_from_any(start)
        result = await plan_route(self.r, mission, pose, str(body.get("planner_id", "")))
        return _json({"ok": True, **result})

    async def post_preview_dryrun(self, request: web.Request) -> web.Response:
        from .preview import DryRun

        body = await self._body(request) or {}
        try:
            mission = await self._mission_from_body(body)
        except MissionValidationError as e:
            return _json({"ok": False, "errors": [x.as_dict() for x in e.errors]}, 400)
        dry = DryRun(
            self.r,
            mission,
            body.get("inputs") or {},
            time_scale=float(body.get("time_scale", 60.0)),
            max_wall_s=min(60.0, float(body.get("max_wall_s", 20.0))),
        )
        result = await dry.execute()
        return _json({"mission": mission.name, **result.as_dict()})

    async def post_map_filters(self, request: web.Request) -> web.Response:
        """Write Nav2 costmap filter masks for this map's zones."""
        from .zonemask import MaskSpec, render_keepout, render_speed_limit, write_mask, zone_bounds

        name = request.match_info["name"]
        mapdef = self.r.store.sites.map(name)
        if mapdef is None:
            return _error("no such map", 404)
        body = await self._body(request) if request.can_read_body else {}
        body = body or {}
        zones = list(mapdef.zones.values())
        if not zones:
            return _error("this map has no zones", 400)
        img = self._map_image(name)
        bounds = zone_bounds(zones, tuple(img.meta()["bounds"]) if img else (-10.0, -10.0, 10.0, 10.0))
        spec = MaskSpec.from_bounds(bounds, float(body.get("resolution", img.resolution if img else 0.05)))
        max_speed = float(body.get("max_speed_mps", 0.5))
        directory = self.r.store.home / "filters"
        out = []
        if any(z.kind == "keepout" for z in zones):
            out.append(write_mask(render_keepout(zones, spec), directory, f"{name}_keepout"))
        if any(z.kind == "speed_limit" and z.speed_mps for z in zones):
            out.append(write_mask(render_speed_limit(zones, spec, max_speed), directory, f"{name}_speed"))
        if not out:
            return _error("no keepout or speed_limit zones to export", 400)
        self.r.events.log("info", f"wrote {len(out)} costmap filter mask(s) for map '{name}' into {directory}")
        return _json({"ok": True, "masks": out, "directory": str(directory)})

    async def get_project(self, request: web.Request) -> web.Response:
        return _json(self.r.export_project(request.query.get("name", "")))

    async def put_project(self, request: web.Request) -> web.Response:
        doc = await self._body(request)
        replace = request.query.get("replace", "false").lower() in ("1", "true", "yes")
        ok, out = await self.r.import_project(doc, replace)
        return _json({"ok": ok, **out}, 200 if ok else 400)

    async def get_robot_pose(self, request: web.Request) -> web.Response:
        st = self.r.backend.robot_state()
        pose = st.pose_dict()
        if pose is None:
            return _error("robot pose unknown", 503)
        return _json(pose)

    async def get_connectors(self, request: web.Request) -> web.Response:
        return _json(self.r.connector_status())

    # ----- autostart services (docs/robot-startup.md) ----------------------------------------

    async def _autostart(self, fn: Any, *args: Any, change: str | None = None, **kwargs: Any) -> web.Response:
        """Run a blocking AutostartManager call off the loop; errors become JSON."""
        from .autostart import AutostartError

        try:
            out = await asyncio.to_thread(fn, *args, **kwargs)
        except AutostartError as e:
            return _json({"error": e.errors[0] if e.errors else "error", "errors": e.errors}, e.status)
        if change:
            self.r.events.emit("autostart.changed", name=change)
        return _json(out)

    def _autostart_disabled(self) -> web.Response | None:
        if not self.r.autostart.enabled:
            msg = "autostart is disabled on this robot (autostart.enabled: false in runner.yaml)"
            return _json({"error": msg, "errors": [msg]}, 403)
        return None

    def _later(self, delay_s: float, fn: Any, *args: Any, **kwargs: Any) -> None:
        """Act on the runner's own service after the response went out."""

        async def go() -> None:
            await asyncio.sleep(delay_s)
            try:
                await asyncio.to_thread(fn, *args, **kwargs)
            except Exception:  # noqa: BLE001
                log.exception("deferred autostart action failed")

        self.r._spawn(go(), "autostart-self")

    async def get_autostart(self, request: web.Request) -> web.Response:
        return await self._autostart(self.r.autostart.overview)

    async def get_autostart_browse(self, request: web.Request) -> web.Response:
        # (a Response is a MutableMapping and falsy when empty: compare with None, not `or`)
        if (denied := self._autostart_disabled()) is not None:
            return denied
        return await self._autostart(self.r.autostart.browse, request.query.get("path") or None)

    async def put_autostart(self, request: web.Request) -> web.Response:
        if (denied := self._autostart_disabled()) is not None:
            return denied
        name = request.match_info["name"]
        body = await self._body(request)
        mgr = self.r.autostart
        is_self = mgr.is_self(name)
        resp = await self._autostart(mgr.put, name, body, defer_restart=is_self, change=name)
        if is_self and resp.status == 200 and isinstance(body, dict) and body.get("start_now") is True:
            self._later(1.0, mgr.action, name, "restart", no_block=True)
        return resp

    async def post_autostart_action(self, request: web.Request) -> web.Response:
        if (denied := self._autostart_disabled()) is not None:
            return denied
        name, verb = request.match_info["name"], request.match_info["verb"]
        mgr = self.r.autostart
        if verb in ("stop", "restart") and mgr.is_self(name):
            resp = await self._autostart(mgr.service, name, change=name)
            if resp.status == 200:
                self._later(1.0, mgr.action, name, verb, no_block=True)
            return resp
        return await self._autostart(mgr.action, name, verb, change=name)

    async def delete_autostart(self, request: web.Request) -> web.Response:
        if (denied := self._autostart_disabled()) is not None:
            return denied
        name = request.match_info["name"]
        mgr = self.r.autostart
        is_self = mgr.is_self(name)
        resp = await self._autostart(mgr.remove, name, stop=not is_self, change=name)
        if is_self and resp.status == 200:
            self._later(1.0, mgr.stop_later, name)
        return resp

    async def get_autostart_log(self, request: web.Request) -> web.Response:
        if (denied := self._autostart_disabled()) is not None:
            return denied
        try:
            lines = int(request.query.get("lines", "200"))
        except ValueError:
            return _json({"error": "lines must be a number", "errors": ["lines must be a number"]}, 400)
        return await self._autostart(self.r.autostart.log, request.match_info["name"], lines)

    async def post_autostart_linger(self, request: web.Request) -> web.Response:
        if (denied := self._autostart_disabled()) is not None:
            return denied
        return await self._autostart(self.r.autostart.enable_linger)

    # ----- hooks / sim ------------------------------------------------------------------------

    async def post_hook(self, request: web.Request) -> web.Response:
        path = request.match_info["path"]
        payload: Any = {}
        if request.can_read_body:
            try:
                payload = await request.json()
            except ValueError:
                payload = {"text": await request.text()}
        if not isinstance(payload, dict):
            payload = {"value": payload}
        payload.setdefault("path", path)
        n = self.r.internal.push_webhook(path, payload)
        if n == 0:
            return _error(f"no mission listens on /hooks/{path}", 404)
        return _json({"ok": True, "listeners": n}, 202)

    async def post_sim_topic(self, request: web.Request) -> web.Response:
        if self.r.sim_topics is None:
            return _error("only available with --sim", 404)
        body = await self._body(request)
        topic = str((body or {}).get("topic", ""))
        payload = (body or {}).get("payload")
        if not topic or not isinstance(payload, dict):
            return _error("need {topic, payload: {...}}")
        n = self.r.sim_topics.push(topic, payload)
        return _json({"ok": True, "listeners": n})

    async def post_sim_robot(self, request: web.Request) -> web.Response:
        b = self.r.backend
        if b.name != "sim":
            return _error("only available with --sim", 404)
        body = await self._body(request) or {}
        from .backends.base import Pose

        st = b.robot_state()
        if any(k in body for k in ("x", "y", "yaw_deg")):
            b.set_pose(Pose(float(body.get("x", st.x or 0)), float(body.get("y", st.y or 0)), float(body.get("yaw_deg", st.yaw_deg or 0))))  # type: ignore[attr-defined]
        if "battery" in body:
            st.battery = float(body["battery"])
        for k in ("fail_next", "blocked"):
            if k in body:
                setattr(b, k, bool(body[k]))
        return _json({"ok": True, "robot": b.robot_state().as_dict()})

    # ----- websocket ---------------------------------------------------------------------------

    async def ws_events(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=20)
        await ws.prepare(request)
        self._ws_clients.add(ws)
        try:
            await ws.send_str(json.dumps({"type": "status", **self.r.status()}, default=str))
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                    except ValueError:
                        continue
                    if data.get("type") == "ping":
                        await ws.send_str('{"type":"pong"}')
                    elif data.get("type") == "status":
                        await ws.send_str(json.dumps({"type": "status", **self.r.status()}, default=str))
                    elif data.get("type") == "live":
                        self._ws_layers[ws] = {str(x) for x in (data.get("layers") or [])}
                        self._sync_live()
                        await ws.send_str(json.dumps({"type": "live.layers", "layers": sorted(self._ws_layers[ws]), "available": self.r.live.available() if self.r.live else {}}))
                elif msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                    break
        finally:
            self._ws_clients.discard(ws)
            self._ws_layers.pop(ws, None)
            self._sync_live()
        return ws

    def _sync_live(self) -> None:
        """The relay subscribes to the union of what the open editors ask for."""
        if self.r.live is None:
            return
        wanted: set[str] = set()
        for layers in self._ws_layers.values():
            wanted |= layers
        self.r.live.set_layers(wanted)

    def _on_event(self, event: dict[str, Any]) -> None:
        if not self._ws_clients:
            return
        payload = json.dumps(event, ensure_ascii=False, default=str)
        status_types = {"run.queued", "run.started", "run.finished", "run.suspended", "run.resumed", "run.paused", "prompt", "prompt.answered", "map.changed", "missions.changed", "sites.changed"}
        status_payload = json.dumps({"type": "status", **self.r.status()}, default=str) if event.get("type") in status_types else None
        for ws in list(self._ws_clients):
            asyncio.ensure_future(self._send(ws, payload, status_payload))

    async def _send(self, ws: web.WebSocketResponse, payload: str, status_payload: str | None) -> None:
        try:
            await ws.send_str(payload)
            if status_payload:
                await ws.send_str(status_payload)
        except Exception:  # noqa: BLE001
            self._ws_clients.discard(ws)


FALLBACK_PAGE = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>mission_runner</title>
<style>
body{margin:0;font:16px/1.4 system-ui,sans-serif;background:#15181d;color:#dfe4ec}
main{max-width:520px;margin:0 auto;padding:16px}
.card{background:#1d2128;border:1px solid #303744;border-radius:10px;padding:12px;margin:12px 0}
button{font:inherit;color:#fff;background:#2b7bd1;border:0;border-radius:8px;padding:10px 14px;cursor:pointer}
button.stop{background:#c62838;width:100%;font-size:22px;font-weight:700;padding:18px}
button.ghost{background:#242933;border:1px solid #303744}
.row{display:flex;align-items:center;gap:8px;padding:6px 0;border-bottom:1px solid #303744}.row:last-child{border:0}
.muted{color:#8b95a5;font-size:13px}.prompt{border-color:#f2b134;background:#2b2411}
pre{white-space:pre-wrap;font-size:12px;color:#8b95a5;max-height:200px;overflow:auto}
</style></head><body><main>
<h2 style="margin:8px 0">mission_runner <span id="state" class="muted"></span></h2>
<button class="stop" onclick="post('/api/stop')">STOP</button>
<div id="run" class="card"></div>
<div id="prompt" class="card prompt" style="display:none"></div>
<div id="missions" class="card"></div>
<div class="card"><div class="muted">Log</div><pre id="log"></pre></div>
</main><script>
const $=id=>document.getElementById(id);let log=[];
async function post(p,b){await fetch(p,{method:'POST',headers:{'Content-Type':'application/json'},body:b?JSON.stringify(b):undefined});}
function render(s){
 $('state').textContent=s.state+(s.robot&&s.robot.battery!=null?' · battery '+Math.round(s.robot.battery*100)+'%':'');
 const r=s.run;$('run').innerHTML=r?`<b>${r.mission}</b> · ${r.status}${r.step?' · '+(r.step.name||r.step.id):''}<div class="muted">${r.feedback?JSON.stringify(r.feedback):''}</div>
 <div style="margin-top:8px"><button class="ghost" onclick="post('/api/runs/${r.id}/${r.status==='paused'?'resume':'pause'}')">${r.status==='paused'?'Resume':'Pause'}</button> <button class="ghost" onclick="post('/api/runs/${r.id}/cancel')">Cancel</button></div>`:'<span class="muted">Nothing running</span>'+(s.queue.length?` · ${s.queue.length} queued`:'');
 const p=s.prompt;$('prompt').style.display=p?'':'none';if(p)$('prompt').innerHTML=`<b>${p.text}</b><div class="muted">${p.default?'Default: '+p.default:''} ${p.expires_at?'· until '+new Date(p.expires_at).toLocaleTimeString():''}</div><div style="margin-top:8px">${p.options.map(o=>`<button onclick="post('/api/prompt/${p.id}/answer',{answer:'${o}'})">${o}</button> `).join('')}</div>`;
}
async function missions(){const ms=await (await fetch('/api/missions')).json();$('missions').innerHTML='<div class="muted">Missions</div>'+ms.filter(m=>m.name!=='global').map(m=>`<div class="row"><div style="flex:1"><b>${m.title||m.name}</b><div class="muted">${(m.triggers||[]).join(', ')||'manual'}</div></div><button onclick="post('/api/missions/${m.name}/run',{})">Run</button></div>`).join('');}
function connect(){const ws=new WebSocket((location.protocol==='https:'?'wss://':'ws://')+location.host+'/api/events');
 ws.onmessage=e=>{const ev=JSON.parse(e.data);if(ev.type==='status')render(ev);else if(ev.type==='log'){log.push(new Date(ev.t*1000).toLocaleTimeString()+' '+ev.level+' '+ev.text);log=log.slice(-60);$('log').textContent=log.join('\\n');}else if(ev.type==='missions.changed')missions();};
 ws.onclose=()=>setTimeout(connect,2000);}
missions();connect();
</script></body></html>"""
