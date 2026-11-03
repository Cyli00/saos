"""saos ui 的本机 HTTP 服务：只读任务目录给出流程状态和每轮可视化数据，并把指标查询与选择转给后台进程。
Local HTTP service of saos ui: serves the workflow state and per-round visual data from the job folder (read only)
and forwards metric queries and selections to the daemon.

与持有 OpticStudio 的后台进程分开运行，界面轮询不占后台进程。后台进程一次只处理一个请求，
它正在跑长操作时，转发的请求返回 BUSY。
Runs apart from the daemon that holds OpticStudio. The daemon serves one request at a time; a forwarded request
returns BUSY while a long operation runs.

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


def live_of(job: jobs.Job) -> dict[str, Any]:
    from saos import live

    path = job.root / jobs.VIEW_FILE
    if not path.is_file():
        raise SaosError(ErrorCode.FILE_NOT_FOUND, "no live view yet: the daemon writes one after the first command that loads or changes the lens")
    return {**_with_section(json.loads(path.read_text(encoding="utf-8"))), "metrics": live.read(job)}


def viz_of(job: jobs.Job, number: int) -> dict[str, Any]:
    record = next((r for r in ledger.load(job.root / ledger.LEDGER_FILE) if r["round"] == number), None)
    if record is None or not record.get("evaluation"):
        raise SaosError(ErrorCode.BAD_ARGS, f"no round {number}")
    path = job.root / record["evaluation"].rsplit("/", 1)[0] / "viz.json"
    if not path.is_file():
        raise SaosError(ErrorCode.FILE_NOT_FOUND, f"round {number} has no viz.json (recorded with --no-viz or before saos ui existed)")
    return _with_section(json.loads(path.read_text(encoding="utf-8")))


def gate_of(job: jobs.Job, current: str | None = None) -> dict[str, Any]:
    """窗口里要用户点确认的关卡：当前步骤是确认需求时给出需求单（demand.confirm_gate），是确认建模说明时给出那份说明
    （model_gate.approval_gate）；都不是时两项为 None。
    The gate the window asks the user to press: the demand (demand.confirm_gate) at demand_confirm, the description
    (model_gate.approval_gate) at model_approve; both None otherwise. current is the snapshot's step when the caller
    already has it, which spares a second snapshot on every poll."""
    from saos import demand, model_gate

    if current is None:
        current = stage.snapshot(job)["current"]
    return {"step": current, "demand": demand.confirm_gate(job.root) if current == "demand_confirm" else None,
            "model": model_gate.approval_gate(job) if current == "model_approve" else None}


def confirm_of(job: jobs.Job, args: dict[str, Any]) -> dict[str, Any]:
    """用户在窗口里按下确认：记录窗口确认（via=window），备注可空；sha 须是窗口显示的那一版（ADR-0020）。
    The user pressed confirm in the window: record a window confirmation with an optional note; sha must be the version shown."""
    from saos import demand, model_gate

    note = str(args.get("note") or "").strip()[:2000]
    gate = args.get("gate")
    if gate not in ("demand", "model"):
        raise SaosError(ErrorCode.BAD_ARGS, "gate must be demand or model")
    pending = gate_of(job)[gate]
    model = str(args.get("model") or "")
    if pending is None or (gate == "model" and pending["model"] != model):
        raise SaosError(ErrorCode.BAD_ARGS, f"nothing to confirm: this {gate} is not waiting for confirmation (already confirmed, or the step moved on)")
    if gate == "demand":
        return {"gate": gate, "record": demand.confirm(job.root, note, via="window", shown_sha=str(args.get("sha") or ""))}
    return {"gate": gate, "record": model_gate.approve(job, model, note, via="window", shown_sha=str(args.get("sha") or ""))}


def revise_of(job: jobs.Job, args: dict[str, Any]) -> dict[str, Any]:
    """用户在窗口里对关卡要求修改（只在参与任务里）：记下原话，关卡保持等待，agent 出新版后再确认（ADR-0021）。
    The user asked for changes at a gate (interactive jobs): their words are kept and the gate keeps waiting."""
    if job.mode != "interactive":
        raise SaosError(ErrorCode.BAD_ARGS, "this job runs autonomously; switch it to interactive in the window first")
    gate = args.get("gate")
    if gate not in ("demand", "model"):
        raise SaosError(ErrorCode.BAD_ARGS, "gate must be demand or model")
    pending = gate_of(job)[gate]
    if pending is None:
        raise SaosError(ErrorCode.BAD_ARGS, f"this {gate} is not waiting for confirmation")
    target = pending["demand_sha"] if gate == "demand" else pending["model"]
    if str(args.get("sha") or "") != (pending["demand_sha"] if gate == "demand" else pending["shown_sha"]):
        raise SaosError(ErrorCode.BAD_ARGS, f"the {gate} changed after the window showed it; read the new version first")
    return {"revision": jobs.record_revision(job, gate, target, str(args.get("note") or ""))}


def open_page(job: jobs.Job, args: dict[str, Any]) -> dict[str, Any]:
    """用系统默认浏览器打开这个任务的一份建模说明页（models/<文件夹>/index.html），别的文件一律拒绝。
    Open one of the job's model pages (models/<folder>/index.html) in the default browser; any other file is refused."""
    import webbrowser

    rel = str(args.get("page") or "")
    path = (job.root / rel).resolve()
    if path.name != "index.html" or path.parent.parent != (job.root / "models").resolve() or not path.is_file():
        raise SaosError(ErrorCode.BAD_ARGS, "page must be models/<folder>/index.html of this job")
    webbrowser.open(path.as_uri())
    return {"opened": rel}


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
        if path == "/api/metrics" and method in ("GET", "POST"):
            if method == "GET":
                return client.request("metrics.list", {"job": job.id}, timeout=10)
            return client.request("metrics.select", {"job": job.id, "items": args.get("items")}, timeout=10)
        if path == "/api/metric" and method == "POST":
            return client.request("metrics.get", {"job": job.id, "id": args.get("id"), "params": args.get("params", {})}, timeout=30)
        if path == "/api/state":
            self.server.seen[job.id] = time.monotonic()
            _mark_closed(job.id, False)
            (self.server.hidden.add if args.get("hidden") == "1" else self.server.hidden.discard)(job.id)
            snap = stage.snapshot(job)
            # 关卡内容随状态一起给，窗口不用每次轮询再要一遍 / the gate comes with the state, so the window needs no second request
            return {**snap, "gate": gate_of(job, snap["current"]), "notice": self.server.notices.get(job.id)}
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
        if path == "/api/gate":
            return gate_of(job)
        if path == "/api/confirm" and method == "POST":
            # 连点两下只记一次：第二次时关卡已过，sha 对不上 / a double click records once: the second finds the gate passed
            with self.server.confirm_lock:
                return confirm_of(job, args)
        if path == "/api/open" and method == "POST":
            return open_page(job, args)
        if path == "/api/revise" and method == "POST":
            with self.server.confirm_lock:
                return revise_of(job, args)
        if path == "/api/mode" and method == "POST":
            # 只能从自主改为参与：agent 不能借窗口把任务切成自主 / only autonomous → interactive
            if args.get("mode") != "interactive":
                raise SaosError(ErrorCode.BAD_ARGS, "the window can only make a job interactive")
            return jobs.make_interactive(job)
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
        self.confirm_lock = threading.Lock()

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
