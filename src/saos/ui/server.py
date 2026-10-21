"""saos ui 的本机 HTTP 服务：只读任务目录给出流程状态和每轮可视化数据；显性调试时把几个只看不改的窗口操作转给后台进程。
Local HTTP service of saos ui: serves the workflow state and per-round visual data from the job folder (read only);
in explicit debugging it forwards a few view-only window operations to the daemon.

与持有 OpticStudio 的后台进程分开运行，界面轮询不占后台进程。后台进程一次只处理一个请求，
它正在跑长操作时，转发的窗口操作会排队到它结束。
Runs apart from the daemon that holds OpticStudio. The daemon serves one request at a time, so a forwarded window
operation waits until a running long operation ends.

只监听 127.0.0.1；/api/ 要带启动时生成的令牌（X-Saos-Token），并核对 Host，防止其他网页借浏览器调用。
Listens on 127.0.0.1 only; /api/ needs the token made at start (X-Saos-Token) and a matching Host header.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from saos import client, config, jobs, ledger
from saos.protocol import ErrorCode, SaosError, fail, jsonable, ok, write_json
from saos.ui import section, stage

log = logging.getLogger("saos.ui")

STATIC = Path(__file__).resolve().parent / "static"
STATIC_TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                ".svg": "image/svg+xml"}
# 窗口每 1.5 s 轮询一次；这么久没有请求说明窗口已关，服务自行退出 / no request for this long means the window is closed
IDLE_EXIT_S = 900
# 显性调试面板可开的分析窗口（AnalysisIDM 名），与后台进程报告的可用类型取交集
# analysis windows the explicit panel offers (AnalysisIDM names), intersected with what the daemon reports
ANALYSES = ("StandardSpot", "FftMtf", "RayFan", "FieldCurvatureAndDistortion", "WavefrontMap", "LongitudinalAberration",
            "LateralColor", "SeidelDiagram", "FftPsf", "HuygensPsf", "FootprintSettings")
VIEWERS = ("cross-section", "3d", "shaded")


def state_file() -> Path:
    return config.daemon_dir() / "ui.json"


def endpoint_file() -> Path:
    # 上次的端口和令牌，退出后保留：重启时沿用，已经开着的窗口不用换地址就能重连
    # the last port and token, kept after exit: a restart reuses them so open windows reconnect at the same address
    return config.daemon_dir() / "ui-endpoint.json"


def closed_file() -> Path:
    # 用户关掉了窗口的任务；窗口再来轮询时去掉 / jobs whose window the user closed; dropped when the window polls again
    return config.daemon_dir() / "ui-closed.json"


def closed_jobs() -> set[str]:
    try:
        return set(json.loads(closed_file().read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        return set()


def _mark_closed(job_id: str, closed: bool) -> None:
    jobs_closed = closed_jobs()
    if (job_id in jobs_closed) != closed:
        write_json(closed_file(), sorted(jobs_closed | {job_id} if closed else jobs_closed - {job_id}))


def read_state() -> dict[str, Any] | None:
    try:
        return json.loads(state_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _explicit() -> None:
    mode = stage._daemon()
    if not mode["running"]:
        raise SaosError(ErrorCode.DAEMON_NOT_RUNNING, "the daemon is not running")
    if mode["mode"] != "extension":
        raise SaosError(ErrorCode.BAD_ARGS, "window controls need the daemon in extension mode (explicit debugging)")


def live_of(job: jobs.Job) -> dict[str, Any]:
    path = job.root / jobs.VIEW_FILE
    if not path.is_file():
        raise SaosError(ErrorCode.FILE_NOT_FOUND, "no live view yet: the daemon writes one after the first command that loads or changes the lens")
    return _with_section(json.loads(path.read_text(encoding="utf-8")))


def viz_of(job: jobs.Job, number: int) -> dict[str, Any]:
    record = next((r for r in ledger.load(job.root / ledger.LEDGER_FILE) if r["round"] == number), None)
    if record is None or not record.get("evaluation"):
        raise SaosError(ErrorCode.BAD_ARGS, f"no round {number}")
    path = job.root / record["evaluation"].rsplit("/", 1)[0] / "viz.json"
    if not path.is_file():
        raise SaosError(ErrorCode.FILE_NOT_FOUND, f"round {number} has no viz.json (recorded with --no-viz or before saos ui existed)")
    return _with_section(json.loads(path.read_text(encoding="utf-8")))


def _with_section(viz: dict[str, Any]) -> dict[str, Any]:
    # 2D 剖面所在的平面；折转系统也能画 2D / the plane of the 2D section, so folded systems get a 2D view too
    if viz.get("surfaces"):
        viz["section"] = section.section_plane(viz)
    return viz


class Handler(BaseHTTPRequestHandler):
    server: "UiServer"

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: D102 - 不往标准错误刷访问日志
        log.debug(fmt, *args)

    def _send(self, code: int, body: bytes, kind: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, message: dict[str, Any], code: int = 200) -> None:
        self._send(code, json.dumps(jsonable(message), ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _allowed_host(self) -> bool:
        port = self.server.server_address[1]
        return self.headers.get("Host") in (f"127.0.0.1:{port}", f"localhost:{port}")

    def _api(self, method: str) -> None:
        if not self._allowed_host() or not secrets.compare_digest(self.headers.get("X-Saos-Token", ""), self.server.token):
            self._json(fail(SaosError(ErrorCode.BAD_ARGS, "forbidden")), 403)
            return
        self.server.touch()
        url = urlparse(self.path)
        query = {k: v[-1] for k, v in parse_qs(url.query).items()}
        body: dict[str, Any] = {}
        if method == "POST":
            length = min(int(self.headers.get("Content-Length") or 0), 1 << 16)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                body = {}
        try:
            result = self._route(method, url.path, {**query, **body})
        except SaosError as exc:
            self._json(fail(exc))
            return
        except Exception as exc:  # noqa: BLE001 - 单个请求出错不影响服务
            log.exception("ui request failed")
            self._json(fail(SaosError(ErrorCode.INTERNAL_ERROR, f"{type(exc).__name__}: {exc}")))
            return
        self._json(ok(result))

    def _route(self, method: str, path: str, args: dict[str, Any]) -> Any:
        if path == "/api/ping":
            # 各任务的窗口多久之前来轮询过、当时是否隐藏（最小化），命令行据此判断窗口是否开着
            # how long ago each job's window polled and whether it was hidden (minimised) then
            now = time.monotonic()
            return {"pid": os.getpid(), "seen_s": {k: round(now - v, 1) for k, v in self.server.seen.items()},
                    "hidden": sorted(self.server.hidden)}
        if path == "/api/jobs":
            return {"jobs": jobs.list_jobs()}
        job = jobs.get_job(str(args.get("job", "")))
        if path == "/api/state":
            self.server.seen[job.id] = time.monotonic()
            _mark_closed(job.id, False)
            (self.server.hidden.add if args.get("hidden") == "1" else self.server.hidden.discard)(job.id)
            return {**stage.snapshot(job), "notice": self.server.notices.get(job.id)}
        if path == "/api/notice" and method == "POST":
            # agent 停下来问用户时留的话（saos ui notify），用户在窗口里点掉 / what the agent left when it stopped to ask
            message = str(args.get("message") or "").strip()
            if message:
                self.server.notices[job.id] = {"message": message[:2000], "time": datetime.now().astimezone().isoformat(timespec="seconds")}
            else:
                self.server.notices.pop(job.id, None)
            return {"notice": self.server.notices.get(job.id)}
        if path == "/api/closed" and method == "POST":
            # 窗口关闭（或刷新）时发来；刷新后的页面马上轮询，标记随即去掉 / sent on close or reload; a reload polls right away
            self.server.seen.pop(job.id, None)
            _mark_closed(job.id, True)
            return {"closed": job.id}
        if path == "/api/live":
            return live_of(job)
        if path == "/api/starts":
            return stage.starts_detail(job)
        if path == "/api/viz":
            try:
                number = int(args.get("round", ""))
            except ValueError as exc:
                raise SaosError(ErrorCode.BAD_ARGS, "round must be an integer") from exc
            return viz_of(job, number)
        if path == "/api/system":
            _explicit()
            return client.request("lens.info", {"job": job.id}, queue=True)
        if path == "/api/analyses":
            _explicit()
            available = set(client.request("lens.analysis", {"job": job.id, "list": True}, queue=True)["types"])
            return {"analyses": [a for a in ANALYSES if a in available]}
        if path == "/api/view" and method == "POST":
            _explicit()
            payload: dict[str, Any] = {"job": job.id}
            if args.get("viewer") is not None:
                if args["viewer"] not in VIEWERS:
                    raise SaosError(ErrorCode.BAD_ARGS, f"viewer must be one of {VIEWERS}")
                payload["viewer"] = args["viewer"]
            if args.get("analysis") is not None:
                if args["analysis"] not in ANALYSES:
                    raise SaosError(ErrorCode.BAD_ARGS, f"analysis must be one of {ANALYSES}")
                payload["analysis"] = args["analysis"]
            return client.request("lens.view", payload, queue=True)
        raise SaosError(ErrorCode.BAD_ARGS, f"unknown endpoint {method} {path}")

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path.startswith("/api/"):
            self._api("GET")
            return
        name = "index.html" if path in ("/", "/index.html") else path.removeprefix("/static/")
        target = (STATIC / name).resolve()
        if STATIC not in target.parents or not target.is_file() or target.suffix not in STATIC_TYPES:
            self._send(404, b"not found", "text/plain")
            return
        self._send(200, target.read_bytes(), STATIC_TYPES[target.suffix])

    def do_POST(self) -> None:  # noqa: N802
        if urlparse(self.path).path.startswith("/api/"):
            self._api("POST")
        else:
            self._send(404, b"not found", "text/plain")


class UiServer(ThreadingHTTPServer):
    daemon_threads = True
    # Windows 的 SO_REUSEADDR 允许两个进程绑同一端口；沿用旧端口时要在被占用时报错
    # on Windows SO_REUSEADDR lets two processes bind one port; reusing the last port must fail when it is taken
    allow_reuse_address = sys.platform != "win32"

    def __init__(self, port: int = 0, token: str | None = None, idle_exit_s: float = IDLE_EXIT_S) -> None:
        super().__init__(("127.0.0.1", port), Handler)
        self.token = token or secrets.token_urlsafe(18)
        self.idle_exit_s = idle_exit_s
        self.last_request = time.monotonic()
        self.seen: dict[str, float] = {}
        self.hidden: set[str] = set()
        self.notices: dict[str, dict[str, str]] = {}

    def touch(self) -> None:
        self.last_request = time.monotonic()

    def watch_idle(self) -> None:
        while True:
            time.sleep(min(30.0, self.idle_exit_s))
            if time.monotonic() - self.last_request > self.idle_exit_s:
                log.info("no request for %.0f s, exiting", self.idle_exit_s)
                self.shutdown()
                return


def _make_server() -> UiServer:
    """沿用上次的端口和令牌；第一次启动或那个端口被占了就换新的，并记下来。
    Reuse the last port and token; take new ones on the first start or when that port is taken, and record them."""
    try:
        last = json.loads(endpoint_file().read_text(encoding="utf-8"))
        server = UiServer(int(last["port"]), str(last["token"]))
    except (OSError, ValueError, KeyError, TypeError):
        server = UiServer()
    write_json(endpoint_file(), {"port": server.server_address[1], "token": server.token})
    return server


def serve() -> int:
    """前台运行服务（saos ui 以后台进程方式调用它）；写 daemon/ui.json，退出时删掉。"""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    server = _make_server()
    port = server.server_address[1]
    write_json(state_file(), {"pid": os.getpid(), "port": port, "token": server.token,
                              "started_at": datetime.now().astimezone().isoformat(timespec="seconds")})
    log.info("saos ui on http://127.0.0.1:%d", port)
    threading.Thread(target=server.watch_idle, daemon=True).start()
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        current = read_state()
        if current and current.get("pid") == os.getpid():
            state_file().unlink(missing_ok=True)
    return 0
