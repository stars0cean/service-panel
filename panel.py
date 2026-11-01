# -*- coding: utf-8 -*-
"""
统一服务面板
------------
一个单机 Web 面板，集中查看 / 控制：
  · 后台服务（Windows 计划任务、常驻进程）
  · 端口映射（本机 → 局域网 → 反向代理 → 公网域名）
  · DDNS 解析状态

启动：python -X utf8 panel.py
访问：http://127.0.0.1:8899
配置：同目录 services.json（面板内可直接编辑）
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
from datetime import datetime
from pathlib import Path

import psutil
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

import deploy as deploy_mod

BASE = Path(__file__).resolve().parent
CONFIG_PATH = BASE / "services.json"
STATIC_DIR = BASE / "static"
LOG_PATH = BASE / "panel.log"

CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008

# 不走系统/环境代理，避免 127.0.0.1 被代理劫持
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

app = FastAPI(title="Service Panel", docs_url=None, redoc_url=None)


# ----------------------------------------------------------------- 工具

def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(msg: str) -> None:
    line = f"[{now_str()}] {msg}\n"
    try:
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > 2 * 1024 * 1024:
            LOG_PATH.write_text("", encoding="utf-8")
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:  # noqa: BLE001
        pass


def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_config_text(text: str) -> dict:
    cfg = json.loads(text)  # 语法错误会抛异常
    if not isinstance(cfg, dict) or "services" not in cfg:
        raise ValueError("配置必须是对象，且包含 services 数组")
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, CONFIG_PATH)
    return cfg


def run(cmd, timeout=20, encoding="gbk"):
    """执行命令，返回 (returncode, stdout_text)。"""
    try:
        r = subprocess.run(
            cmd, capture_output=True, timeout=timeout,
            creationflags=CREATE_NO_WINDOW, shell=isinstance(cmd, str),
        )
        return r.returncode, r.stdout.decode(encoding, "replace")
    except subprocess.TimeoutExpired:
        return 124, "命令超时"
    except FileNotFoundError as e:
        return 127, f"命令不存在：{e}"
    except Exception as e:  # noqa: BLE001
        return 1, f"{type(e).__name__}: {e}"


# ----------------------------------------------------------------- 探测

def _read_key(spec: dict) -> str | None:
    p = spec.get("api_key_file")
    if not p:
        return None
    try:
        return Path(p).read_text(encoding="utf-8").strip()
    except Exception:  # noqa: BLE001
        return None


def primary_v4() -> str | None:
    """本机主局域网 IPv4（排除 Tailscale / 链路本地）。"""
    best = None
    for name, addrs in psutil.net_if_addrs().items():
        if "tailscale" in name.lower():
            continue
        for a in addrs:
            if a.family != socket.AF_INET:
                continue
            ip = a.address
            if ip.startswith(("127.", "169.254.")):
                continue
            if ip.startswith(("192.168.", "10.")) or ip.startswith("172."):
                return ip
            best = best or ip
    return best


def _resolve_host(spec: dict) -> tuple[str | None, str | None]:
    """解析探测目标。支持：host=@self（本机主 IP）、resolve=<域名>（解析到 IPv6/IPv4）。"""
    if spec.get("resolve"):
        domain = spec["resolve"]
        fam = socket.AF_INET6 if (spec.get("family") or "INET6") == "INET6" else socket.AF_INET
        try:
            infos = socket.getaddrinfo(domain, None, fam)
            return infos[0][4][0], domain
        except Exception:  # noqa: BLE001
            return None, domain
    host = spec.get("host", "127.0.0.1")
    if host == "@self":
        host = primary_v4() or "127.0.0.1"
    return host, host


def probe_http(spec: dict) -> dict:
    url = spec["url"]
    if "@self" in url:
        url = url.replace("@self", primary_v4() or "127.0.0.1")
    timeout = float(spec.get("timeout", 4))
    expect = list(spec.get("expect") or [200])
    degraded_on = list(spec.get("degraded_on") or [])
    headers = {"User-Agent": "ServicePanel/1.0"}
    headers.update(spec.get("headers") or {})
    hk = spec.get("api_key_header")
    if hk:
        k = _read_key(spec)
        if k:
            headers[hk] = k

    t0 = time.perf_counter()
    try:
        req = urllib.request.Request(url, headers=headers)
        with _OPENER.open(req, timeout=timeout) as resp:
            code = resp.status
            resp.read(512)
        ms = int((time.perf_counter() - t0) * 1000)
        if code in expect:
            return {"state": "online", "ms": ms, "detail": f"HTTP {code}"}
        if code in degraded_on:
            return {"state": "degraded", "ms": ms,
                    "detail": spec.get("degraded_hint") or f"HTTP {code}"}
        return {"state": "offline", "ms": ms, "detail": f"HTTP {code}（非预期）"}
    except urllib.error.HTTPError as e:
        ms = int((time.perf_counter() - t0) * 1000)
        if e.code in degraded_on:
            return {"state": "degraded", "ms": ms,
                    "detail": spec.get("degraded_hint") or f"HTTP {e.code}"}
        return {"state": "offline", "ms": ms, "detail": f"HTTP {e.code}"}
    except urllib.error.URLError as e:
        return {"state": "offline", "ms": None, "detail": f"连不上（{e.reason}）"}
    except socket.timeout:
        return {"state": "offline", "ms": None, "detail": f"超时 >{timeout:g}s"}
    except Exception as e:  # noqa: BLE001
        return {"state": "offline", "ms": None, "detail": f"{type(e).__name__}"}


def probe_tcp(spec: dict) -> dict:
    host, shown = _resolve_host(spec)
    port = int(spec["port"])
    timeout = float(spec.get("timeout", 3))
    if not host:
        return {"state": "offline", "ms": None, "detail": f"{shown} 解析不出地址"}
    t0 = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            ms = int((time.perf_counter() - t0) * 1000)
        return {"state": "online", "ms": ms, "detail": f"{host}:{port} 可连"}
    except socket.timeout:
        return {"state": "offline", "ms": None, "detail": f"{host}:{port} 连接超时 >{timeout:g}s"}
    except ConnectionRefusedError:
        return {"state": "offline", "ms": None, "detail": f"{host}:{port} 拒绝连接"}
    except OSError as e:
        return {"state": "offline", "ms": None, "detail": f"不通（{e.strerror or e}）"}


def probe_dns(spec: dict) -> dict:
    domain = spec["domain"]
    # 注意：probe.type 已经被用来表示探测类型（dns），记录类型单独用 record_type
    rtype = (spec.get("record_type") or "AAAA").upper()
    t0 = time.perf_counter()
    try:
        infos = socket.getaddrinfo(domain, None,
                                    socket.AF_INET6 if rtype == "AAAA" else socket.AF_INET)
        addrs = sorted({i[4][0] for i in infos})
        ms = int((time.perf_counter() - t0) * 1000)
        if not addrs:
            return {"state": "offline", "ms": ms, "detail": "无解析结果", "addrs": []}
        return {"state": "online", "ms": ms, "detail": f"{len(addrs)} 条 {rtype}", "addrs": addrs}
    except socket.gaierror as e:
        return {"state": "offline", "ms": None, "detail": f"解析失败（{e.strerror}）", "addrs": []}
    except Exception as e:  # noqa: BLE001
        return {"state": "offline", "ms": None, "detail": f"{type(e).__name__}", "addrs": []}


BRIDGE = BASE / "_task_bridge.ps1"
PS_EXE = "powershell.exe"

_TASK_STATE = {"ready": "ready", "running": "running", "queued": "queued",
               "disabled": "disabled", "unknown": "unknown", "missing": "missing"}


def ps_bridge(action: str, names: list[str], timeout: float = 30) -> tuple[bool, str]:
    """调用 PowerShell 计划任务桥（不使用 schtasks.exe）。"""
    if not names:
        return False, "没有任务名"
    cmd = [PS_EXE, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(BRIDGE),
           "-Action", action, "-Names", ",".join(names)]
    code, out = run(cmd, timeout=timeout, encoding="utf-8")
    return code == 0, out.strip()


def query_tasks(names: list[str]) -> dict[str, dict]:
    """一次性查询多个计划任务，返回 name -> info。"""
    names = [n for n in dict.fromkeys(names) if n]
    if not names:
        return {}
    ok, out = ps_bridge("query", names)
    if not ok:
        return {}
    try:
        data = json.loads(out or "[]")
    except json.JSONDecodeError:
        return {}
    if isinstance(data, dict):
        data = [data]
    res: dict[str, dict] = {}
    for item in data:
        n = item.get("name")
        if not n:
            continue
        st = str(item.get("state") or "Unknown").strip().lower()
        res[n] = {"exists": bool(item.get("exists")),
                  "state": _TASK_STATE.get(st, "unknown"),
                  "raw": item.get("state"),
                  "last_run": item.get("last") or None,
                  "last_result": item.get("result")}
    return res


def probe_task(spec: dict, svc: dict, tinfo: dict | None = None) -> dict:
    name = svc.get("task")
    if not name:
        return {"state": "unknown", "ms": None, "detail": "未配置计划任务名"}
    info = (tinfo or {}).get(name) or {"exists": False, "state": "missing"}
    if not info.get("exists"):
        return {"state": "offline", "ms": None, "detail": f"计划任务 {name} 不存在", "task": info}
    st = info["state"]
    state = "online" if st in ("ready", "running", "queued") else "offline"
    bits = [f"计划任务 {info.get('raw') or st}"]
    if info.get("last_run"):
        bits.append(f"上次 {info['last_run']}")
    lr = info.get("last_result")
    if lr not in (None, 0, "0", 267009):
        bits.append(f"上次结果 {lr}")
    return {"state": state, "ms": None, "detail": "，".join(bits), "task": info}


def run_probe(spec: dict, svc: dict | None = None, tinfo: dict | None = None) -> dict:
    if not spec:
        return {"state": "unknown", "ms": None, "detail": "未配置探测"}
    t = spec.get("type", "tcp")
    try:
        if t == "http":
            return probe_http(spec)
        if t == "tcp":
            return probe_tcp(spec)
        if t == "dns":
            return probe_dns(spec)
        if t == "task":
            return probe_task(spec, svc or spec, tinfo)
        if t == "none":
            return {"state": "unknown", "ms": None, "detail": "仅登记"}
        return {"state": "unknown", "ms": None, "detail": f"未知探测类型 {t}"}
    except Exception as e:  # noqa: BLE001
        return {"state": "unknown", "ms": None, "detail": f"探测异常 {type(e).__name__}: {e}"}


def find_procs(patterns) -> list[dict]:
    found = []
    if not patterns:
        return found
    pats = [p.lower() for p in patterns]
    for p in psutil.process_iter(["pid", "name", "cmdline", "create_time"]):
        try:
            cmd = " ".join(p.info["cmdline"] or [])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if not cmd:
            continue
        low = cmd.lower()
        if any(pat in low for pat in pats):
            try:
                age = int(time.time() - p.info["create_time"])
            except Exception:  # noqa: BLE001
                age = None
            found.append({"pid": p.info["pid"], "age": age,
                          "name": p.info["name"], "cmd": cmd[:400]})
    found.sort(key=lambda x: x["age"] if x["age"] is not None else 1 << 30)
    return found


def local_ip_set() -> dict:
    v4, v6 = [], []
    for name, addrs in psutil.net_if_addrs().items():
        if name.startswith(("Loopback", "蓝牙", "本地连接")):
            continue
        for a in addrs:
            if a.family == socket.AF_INET and not a.address.startswith(("127.", "169.254.")):
                v4.append({"iface": name, "addr": a.address})
            elif a.family == socket.AF_INET6:
                ip = a.address.split("%")[0]
                if ip.startswith(("fe80", "::1")):
                    continue
                kind = "ula" if ip.startswith("fd") else ("lo" if ip.startswith("::1") else "global")
                v6.append({"iface": name, "addr": ip, "kind": kind})
    return {"v4": v4, "v6": v6}


def _prefix6(addr: str, groups: int = 4) -> str:
    return ":".join(addr.split(":")[:groups])


# ----------------------------------------------------------------- 缓存

class Snapshot:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict = {"ready": False, "ts": None, "services": [], "mappings": [], "host": {}}
        self._refreshing = False
        self._pending = False
        self.refresh_sec = 5

    def get(self) -> dict:
        with self._lock:
            return self._data

    @property
    def ready(self) -> bool:
        with self._lock:
            return bool(self._data.get("ready"))

    def build(self) -> dict:
        cfg = load_config()
        host = local_ip_set()
        v6_prefixes = {_prefix6(x["addr"]) for x in host["v6"] if x["kind"] == "global"}

        want_tasks = [s.get("task") for s in cfg.get("services", [])
                      if s.get("task") and s.get("kind") == "task"]
        tinfo = query_tasks(want_tasks)

        services = []
        for svc in cfg.get("services", []):
            view = dict(svc)
            view.pop("cmd", None)
            pr = run_probe(svc.get("probe") or {}, svc, tinfo)
            procs = find_procs(svc.get("match"))
            view.update({
                "state": pr.get("state", "unknown"),
                "ms": pr.get("ms"),
                "detail": pr.get("detail"),
                "pids": [p["pid"] for p in procs],
                "proc_count": len(procs),
                "uptime": procs[0]["age"] if procs else None,
                "has_cmd": bool(svc.get("cmd") or svc.get("task")),
            })
            view.pop("probe", None)
            view.pop("match", None)
            log_files = []
            for lp in svc.get("logs") or []:
                p = Path(lp)
                log_files.append({
                    "path": lp, "name": p.name,
                    "exists": p.exists(),
                    "size": p.stat().st_size if p.exists() else 0,
                })
            view["logs"] = log_files
            services.append(view)

        mappings = []
        for mp in cfg.get("mappings", []):
            segs = []
            for seg in mp.get("segments", []):
                pr = run_probe(seg.get("probe") or {})
                segs.append({"label": seg.get("label"), "hint": seg.get("hint"),
                             "state": pr.get("state"), "ms": pr.get("ms"),
                             "detail": pr.get("detail")})
            ddns = None
            d = mp.get("ddns")
            if d:
                pr = probe_dns({"domain": d["record"], "record_type": d.get("type", "AAAA")})
                addrs = pr.get("addrs") or []
                same_prefix = bool(v6_prefixes) and any(
                    _prefix6(a) in v6_prefixes for a in addrs
                ) if d.get("type", "AAAA") == "AAAA" else None
                ddns = {"record": d["record"], "type": d.get("type", "AAAA"),
                        "owner": d.get("owner"), "state": pr.get("state"),
                        "ms": pr.get("ms"), "detail": pr.get("detail"),
                        "addrs": addrs, "prefix_match": same_prefix}
            states = [s["state"] for s in segs]
            if "offline" in states:
                overall = "offline"
            elif "degraded" in states:
                overall = "degraded"
            elif all(s == "online" for s in states):
                overall = "online"
            else:
                overall = "unknown"
            mappings.append({
                "id": mp.get("id"), "name": mp.get("name"), "group": mp.get("group"),
                "desc": mp.get("desc"), "access": mp.get("access"),
                "segments": segs, "ddns": ddns, "state": overall,
            })

        return {
            "ready": True, "ts": now_str(),
            "services": services, "mappings": mappings, "host": host,
            "panel": cfg.get("panel", {}),
        }

    def refresh(self) -> None:
        if self._refreshing:
            # 正在跑一轮，标记一下，等它结束后立刻再来一轮（改配置后不用等下一个周期）
            self._pending = True
            return
        self._refreshing = True
        try:
            data = self.build()
            with self._lock:
                self._data = data
        except Exception as e:  # noqa: BLE001
            log(f"快照刷新失败：{type(e).__name__}: {e}")
            with self._lock:
                self._data = dict(self._data)
                self._data["last_error"] = f"{type(e).__name__}: {e}\n{traceback.format_exc()[-800:]}"
        finally:
            self._refreshing = False
            if self._pending:
                self._pending = False
                threading.Thread(target=self.refresh, daemon=True).start()

    def loop(self) -> None:
        while True:
            self.refresh()
            try:
                sec = float(load_config().get("panel", {}).get("refresh_sec", 5))
            except Exception:  # noqa: BLE001
                sec = 5
            self.refresh_sec = max(2, sec)
            time.sleep(self.refresh_sec)


SNAP = Snapshot()


# ----------------------------------------------------------------- 控制

def svc_by_id(sid: str) -> dict:
    cfg = load_config()
    for s in cfg.get("services", []):
        if s.get("id") == sid:
            return s
    raise HTTPException(404, f"没有 id={sid} 的服务")


def _stop_processes(svc: dict) -> list[str]:
    notes = []
    procs = find_procs(svc.get("match"))
    if not procs:
        return ["未发现匹配进程"]
    for p in procs:
        try:
            proc = psutil.Process(p["pid"])
            proc.terminate()
            try:
                proc.wait(timeout=8)
                notes.append(f"已结束 pid={p['pid']}")
            except psutil.TimeoutExpired:
                proc.kill()
                notes.append(f"强杀 pid={p['pid']}")
        except psutil.NoSuchProcess:
            notes.append(f"pid={p['pid']} 已退出")
        except psutil.AccessDenied:
            notes.append(f"pid={p['pid']} 权限不足")
    return notes


def _start(svc: dict) -> list[str]:
    notes = []
    task = svc.get("task")
    if task and svc.get("kind") == "task":
        ok, out = ps_bridge("start", [task])
        if ok:
            notes.append(f"已触发计划任务 {task}")
        else:
            notes.append(f"计划任务启动失败：{out[:160]}")
    if not notes and svc.get("cmd"):
        try:
            subprocess.Popen(
                svc["cmd"], shell=True, cwd=svc.get("cwd") or str(BASE),
                creationflags=DETACHED_PROCESS | CREATE_NO_WINDOW,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, close_fds=True,
            )
            notes.append("已直接拉起进程")
        except Exception as e:  # noqa: BLE001
            notes.append(f"拉起失败：{e}")
    if not notes:
        notes.append("该服务没有配置启动方式（task 或 cmd）")
    return notes


def _stop(svc: dict) -> list[str]:
    notes = []
    task = svc.get("task")
    if task and svc.get("kind") == "task":
        ok, out = ps_bridge("stop", [task])
        if ok:
            notes.append(f"已停止计划任务 {task}")
        else:
            notes.append(f"计划任务停止失败：{out[:160]}")
    notes.extend(_stop_processes(svc))
    if not notes:
        notes.append("没有可停止的目标")
    return notes


# ----------------------------------------------------------------- 路由

@app.middleware("http")
async def only_localhost(request: Request, call_next):
    host = request.client.host if request.client else ""
    if host not in ("127.0.0.1", "::1", "localhost"):
        return JSONResponse({"error": "只允许本机访问"}, status_code=403)
    return await call_next(request)


@app.get("/api/ping")
def ping():
    return {"ok": True, "ts": now_str()}


@app.get("/api/state")
def state():
    data = SNAP.get()
    return JSONResponse(data)


@app.post("/api/refresh")
def refresh_now():
    threading.Thread(target=SNAP.refresh, daemon=True).start()
    return {"ok": True}


@app.post("/api/service/{sid}/{action}")
def service_action(sid: str, action: str):
    svc = svc_by_id(sid)
    if action not in ("start", "stop", "restart"):
        raise HTTPException(400, "action 只能是 start / stop / restart")
    notes: list[str] = []
    try:
        if action in ("stop", "restart"):
            notes.extend(_stop(svc))
            if action == "restart":
                time.sleep(1.5)
        if action in ("start", "restart"):
            notes.extend(_start(svc))
    except Exception as e:  # noqa: BLE001
        notes.append(f"执行出错：{type(e).__name__}: {e}")
    threading.Thread(target=SNAP.refresh, daemon=True).start()
    return {"ok": True, "action": action, "id": sid, "notes": notes, "ts": now_str()}


@app.get("/api/log")
def get_log(path: str, lines: int = 200):
    cfg = load_config()
    allowed = set()
    for s in cfg.get("services", []):
        for lp in s.get("logs") or []:
            allowed.add(str(Path(lp).resolve()).lower())
    target = Path(path).resolve()
    if str(target).lower() not in allowed:
        raise HTTPException(403, "该路径不在日志白名单内")
    if not target.exists():
        return {"ok": False, "error": "文件不存在", "lines": []}
    lines = max(10, min(2000, lines))
    size = target.stat().st_size
    with open(target, "rb") as f:
        if size > 512 * 1024:
            f.seek(size - 512 * 1024)
            f.readline()
        raw = f.read()
    text = raw.decode("utf-8", "replace")
    return {"ok": True, "path": str(target), "size": size,
            "mtime": datetime.fromtimestamp(target.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            "lines": text.splitlines()[-lines:]}


@app.get("/api/config")
def get_config():
    return {"path": str(CONFIG_PATH), "text": CONFIG_PATH.read_text(encoding="utf-8")}


@app.put("/api/config")
async def put_config(request: Request):
    body = await request.json()
    text = body.get("text", "")
    try:
        save_config_text(text)
    except json.JSONDecodeError as e:
        raise HTTPException(400, f"JSON 语法错误：第 {e.lineno} 行 {e.msg}") from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    threading.Thread(target=SNAP.refresh, daemon=True).start()
    return {"ok": True}


@app.post("/api/open")
async def open_path(request: Request):
    body = await request.json()
    what = body.get("what")
    if what == "config":
        os.startfile(str(CONFIG_PATH))
        return {"ok": True}
    if what == "dir":
        os.startfile(str(BASE))
        return {"ok": True}
    raise HTTPException(400, "未知目标")


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


# ----------------------------------------------------------------- 设置

PROBE_TYPES = ["http", "tcp", "dns", "task", "none"]
KINDS = ["task", "process", "external"]
ACTIONS = ["start", "stop", "restart"]
STARTUP_TASK = "ServicePanel"


def _txt(v) -> str:
    return str(v if v is not None else "").strip()


def _num(v, default=None):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def validate_config(cfg) -> tuple[bool, list[str]]:
    """结构校验，返回 (ok, errors)。只挡会导致面板跑不起来的错误。"""
    errs: list[str] = []
    if not isinstance(cfg, dict):
        return False, ["配置最外层必须是 JSON 对象"]

    p = cfg.get("panel")
    if p is None:
        cfg["panel"] = {}
    elif not isinstance(p, dict):
        errs.append("panel 必须是对象")
        p = {}

    port = _num((p or {}).get("port", 8899), 8899)
    if not 1024 <= port <= 65535:
        errs.append("面板端口需在 1024–65535 之间")
    rs = (p or {}).get("refresh_sec", 5)
    try:
        rs = float(rs)
        if not 2 <= rs <= 600:
            errs.append("刷新间隔需在 2–600 秒之间")
    except (TypeError, ValueError):
        errs.append("刷新间隔必须是数字")

    svcs = cfg.get("services")
    if not isinstance(svcs, list):
        return False, errs + ["services 必须是数组"]
    seen: set[str] = set()
    for i, s in enumerate(svcs):
        tag = f"服务 #{i + 1}"
        if not isinstance(s, dict):
            errs.append(f"{tag} 不是对象")
            continue
        sid = _txt(s.get("id"))
        if not sid:
            errs.append(f"{tag} 缺少 id")
        elif not re.fullmatch(r"[A-Za-z0-9_\-]{1,48}", sid):
            errs.append(f"{tag} 的 id「{sid}」只能用字母数字和下划线短横线")
        elif sid in seen:
            errs.append(f"服务 id 重复：{sid}")
        else:
            seen.add(sid)
            tag = f"服务「{_txt(s.get('name')) or sid}」"
        if not _txt(s.get("name")):
            errs.append(f"{tag} 缺少名称")
        kind = s.get("kind", "external")
        if kind not in KINDS:
            errs.append(f"{tag} 托管方式只能是 {' / '.join(KINDS)}")
        if kind == "task" and not _txt(s.get("task")):
            errs.append(f"{tag} 选了计划任务却没填任务名")
        pr = s.get("probe")
        if pr is not None and not isinstance(pr, dict):
            errs.append(f"{tag} 的 probe 必须是对象")
        elif isinstance(pr, dict):
            t = pr.get("type", "tcp")
            if t not in PROBE_TYPES:
                errs.append(f"{tag} 探测类型「{t}」不支持")
            if t == "http" and not _txt(pr.get("url")):
                errs.append(f"{tag} 用了 HTTP 探测但没填 URL")
            if t == "tcp" and not _num(pr.get("port")):
                errs.append(f"{tag} 用了 TCP 探测但没填端口")
            if t == "dns" and not _txt(pr.get("domain")):
                errs.append(f"{tag} 用了 DNS 探测但没填域名")
        lg = s.get("logs")
        if lg is not None and not isinstance(lg, list):
            errs.append(f"{tag} 的日志必须是数组")
        mt = s.get("match")
        if mt is not None and not isinstance(mt, list):
            errs.append(f"{tag} 的进程关键字必须是数组")

    maps = cfg.get("mappings")
    if maps is not None and not isinstance(maps, list):
        errs.append("mappings 必须是数组")
    elif isinstance(maps, list):
        mseen: set[str] = set()
        for i, m in enumerate(maps):
            tag = f"映射 #{i + 1}"
            if not isinstance(m, dict):
                errs.append(f"{tag} 不是对象")
                continue
            mid = _txt(m.get("id"))
            if not mid:
                errs.append(f"{tag} 缺少 id")
            elif not re.fullmatch(r"[A-Za-z0-9_\-]{1,48}", mid):
                errs.append(f"{tag} 的 id「{mid}」只能用字母数字和下划线短横线")
            elif mid in mseen:
                errs.append(f"映射 id 重复：{mid}")
            else:
                mseen.add(mid)
            if not _txt(m.get("name")):
                errs.append(f"{tag} 缺少名称")
            segs = m.get("segments")
            if not isinstance(segs, list) or not segs:
                errs.append(f"{tag} 至少要有一个分段")
            else:
                for j, sg in enumerate(segs):
                    if not isinstance(sg, dict):
                        errs.append(f"{tag} 的第 {j + 1} 段不是对象")
                        continue
                    if not _txt(sg.get("label")):
                        errs.append(f"{tag} 的第 {j + 1} 段缺少标签")
                    sp = sg.get("probe")
                    if isinstance(sp, dict):
                        st = sp.get("type", "tcp")
                        if st not in PROBE_TYPES:
                            errs.append(f"{tag} 第 {j + 1} 段探测类型「{st}」不支持")
                        if st == "http" and not _txt(sp.get("url")):
                            errs.append(f"{tag} 第 {j + 1} 段缺 URL")
                        if st == "tcp" and not _num(sp.get("port")):
                            errs.append(f"{tag} 第 {j + 1} 段缺端口")
    return (len(errs) == 0), errs


def system_info() -> dict:
    proc = psutil.Process()
    try:
        created = proc.create_time()
        started = datetime.fromtimestamp(created).strftime("%Y-%m-%d %H:%M:%S")
        uptime = int(time.time() - created)
    except Exception:  # noqa: BLE001
        started, uptime = None, None
    t = query_tasks([STARTUP_TASK]).get(STARTUP_TASK) or {}
    return {
        "python": sys.version.split()[0],
        "exe": sys.executable,
        "pid": os.getpid(),
        "started": started,
        "uptime": uptime,
        "base": str(BASE),
        "config_path": str(CONFIG_PATH),
        "log_path": str(LOG_PATH),
        "autostart": bool(t.get("exists")) and t.get("state") != "disabled",
        "autostart_exists": bool(t.get("exists")),
        "autostart_state": t.get("raw"),
        "autostart_task": STARTUP_TASK,
    }


@app.get("/api/settings")
def get_settings():
    cfg = load_config()
    names = [s.get("task") for s in cfg.get("services", []) if _txt(s.get("task"))]
    known = query_tasks(names)
    return {
        "config": cfg,
        "meta": {
            "probe_types": PROBE_TYPES,
            "kinds": KINDS,
            "actions": ACTIONS,
            "known_tasks": sorted(known.keys()),
        },
        "system": system_info(),
        "path": str(CONFIG_PATH),
    }


@app.put("/api/settings")
async def put_settings(request: Request):
    body = await request.json()
    cfg = body.get("config")
    ok, errs = validate_config(cfg)
    if not ok:
        return JSONResponse({"ok": False, "errors": errs}, status_code=400)

    old = {}
    try:
        old = load_config()
    except Exception:  # noqa: BLE001
        pass
    if CONFIG_PATH.exists():
        try:
            shutil.copy2(CONFIG_PATH, CONFIG_PATH.with_suffix(".json.bak"))
        except Exception as e:  # noqa: BLE001
            log(f"配置备份失败：{e}")

    text = json.dumps(cfg, ensure_ascii=False, indent=2) + "\n"
    try:
        save_config_text(text)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"写入失败：{e}") from e

    log("配置已更新")
    threading.Thread(target=SNAP.refresh, daemon=True).start()

    notes = []
    old_panel = (old or {}).get("panel") or {}
    new_panel = cfg.get("panel") or {}
    if _num(old_panel.get("port"), 8899) != _num(new_panel.get("port"), 8899):
        notes.append("端口已改，需要重启面板才生效")

    # 明确挂到系统计划任务上的服务，改动后提示
    return {"ok": True, "notes": notes, "path": str(CONFIG_PATH)}


@app.post("/api/probe_test")
async def probe_test(request: Request):
    """表单里「测试」按钮用：不改配置，只按给定 probe 探一次。"""
    body = await request.json()
    spec = body.get("probe") or {}
    svc = body.get("service") or {}
    tinfo = query_tasks([svc.get("task")]) if _txt(svc.get("task")) else {}
    t0 = time.perf_counter()
    res = run_probe(spec, svc, tinfo)
    res["elapsed"] = int((time.perf_counter() - t0) * 1000)
    return res


@app.post("/api/system/autostart")
async def set_autostart(request: Request):
    body = await request.json()
    enable = bool(body.get("enabled"))
    if enable:
        ok, out = run([PS_EXE, "-NoProfile", "-ExecutionPolicy", "Bypass",
                       "-File", str(BASE / "register_panel_task.ps1")],
                      timeout=60, encoding="utf-8")
        if not ok:
            return JSONResponse({"ok": False, "detail": out[-500:]}, status_code=500)
        return {"ok": True, "detail": "已注册开机自启", "system": system_info()}
    ok, out = run([PS_EXE, "-NoProfile", "-Command",
                   f"Unregister-ScheduledTask -TaskName '{STARTUP_TASK}' -Confirm:$false"],
                  timeout=60, encoding="utf-8")
    if not ok:
        return JSONResponse({"ok": False, "detail": out[-500:]}, status_code=500)
    return {"ok": True, "detail": "已取消开机自启（当前进程未受影响）", "system": system_info()}


@app.post("/api/system/panel")
async def panel_control(request: Request):
    body = await request.json()
    action = body.get("action")
    if action == "restart":
        subprocess.Popen(
            [PS_EXE, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
             str(BASE / "_panel_restart.ps1")],
            creationflags=DETACHED_PROCESS | CREATE_NO_WINDOW,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, close_fds=True,
        )
        log("收到重启请求")
        return {"ok": True, "detail": "2 秒后重启，稍等几秒刷新页面"}
    raise HTTPException(400, "只支持 restart")


# ------------------------------------------------------------------ 部署

@app.get("/api/hw")
def api_hw(force: int = 0):
    """硬件检测。force=1 强制重测（会重跑 torch，5~15 秒）。"""
    try:
        cfg = load_config()
        hw = deploy_mod.hardware(cfg, force=bool(force))
        a = deploy_mod.assess(hw, (cfg.get("deploy") or {}).get("requirements"))
        return {"ok": True, "hw": hw, "assess": a}
    except Exception as e:  # noqa: BLE001
        log(f"硬件检测失败：{e}")
        return JSONResponse({"ok": False, "detail": f"{type(e).__name__}: {e}"}, status_code=500)


@app.get("/api/deploy/plan")
def api_deploy_plan():
    """部署计划：每步当前是否已就绪。"""
    try:
        cfg = load_config()
        hw = deploy_mod.hardware(cfg)
        a = deploy_mod.assess(hw, (cfg.get("deploy") or {}).get("requirements"))
        return {"ok": True, "hw": hw, "assess": a,
                "steps": deploy_mod.plan(cfg, hw),
                "running": deploy_mod.running_job()}
    except Exception as e:  # noqa: BLE001
        log(f"生成部署计划失败：{e}")
        return JSONResponse({"ok": False, "detail": f"{type(e).__name__}: {e}"}, status_code=500)


@app.post("/api/deploy/run")
async def api_deploy_run(request: Request):
    try:
        raw = await request.body()
    except Exception:  # noqa: BLE001
        raw = b""
    if not raw:
        body = {}
    else:
        try:
            body = json.loads(raw)
        except Exception:  # noqa: BLE001
            return JSONResponse({"ok": False, "detail": "请求体不是合法 JSON"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"ok": False, "detail": "请求体必须是 JSON 对象"}, status_code=400)
    dry = bool(body.get("dry"))
    run = deploy_mod.running_job()
    if run:
        return JSONResponse({"ok": False, "detail": "已有部署任务在执行"}, status_code=409)
    try:
        cfg = load_config()
        hw = deploy_mod.hardware(cfg)
        a = deploy_mod.assess(hw, (cfg.get("deploy") or {}).get("requirements"))
        if a["level"] == "block" and not dry:
            return JSONResponse(
                {"ok": False, "detail": a["verdict"],
                 "blocks": [i for i in a["items"] if i["level"] == "block"]},
                status_code=400)
        job = deploy_mod.start_job(cfg, dry)
    except Exception as e:  # noqa: BLE001
        log("启动部署失败：\n" + traceback.format_exc()[-1000:])
        return JSONResponse({"ok": False, "detail": f"{type(e).__name__}: {e}"}, status_code=500)
    log(f"启动部署任务 {job.id}（dry={dry}）")
    return {"ok": True, "job": job.id, "dry": dry}


@app.get("/api/deploy/job/{jid}")
def api_deploy_job(jid: str, since: int = 0):
    d = deploy_mod.get_job(jid, since)
    if not d:
        return JSONResponse({"ok": False, "detail": "任务不存在或已过期"}, status_code=404)
    return d


@app.post("/api/deploy/cancel/{jid}")
def api_deploy_cancel(jid: str):
    j = deploy_mod.JOBS.get(jid)
    if not j:
        return JSONResponse({"ok": False, "detail": "任务不存在"}, status_code=404)
    j.cancel()
    return {"ok": True}


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")



def main() -> None:
    log("进程启动")
    cfg = load_config()
    p = cfg.get("panel", {})
    host = p.get("bind", "127.0.0.1")
    port = int(p.get("port", 8899))

    # 已经在跑就直接开浏览器
    try:
        with _OPENER.open(f"http://127.0.0.1:{port}/api/ping", timeout=1.5) as r:
            if r.status == 200:
                log(f"面板已在 {port} 运行，不再重复启动")
                os.startfile(f"http://127.0.0.1:{port}/")
                return
    except Exception:  # noqa: BLE001
        pass

    log(f"监听 http://{host}:{port}")
    threading.Thread(target=SNAP.loop, daemon=True).start()
    try:
        uvicorn.run(app, host=host, port=port, log_level="warning", access_log=False)
    except Exception:  # noqa: BLE001
        log("uvicorn 异常：\n" + traceback.format_exc())
        raise
    finally:
        log("进程退出")


if __name__ == "__main__":
    main()
