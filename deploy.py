# -*- coding: utf-8 -*-
"""
一键部署 + 硬件能力评估
----------------------
给面板提供三件事：

  1. 硬件检测（GPU / 显存 / 驱动 / 计算能力 / 内存 / 磁盘 / torch）
  2. 能力评估（对照 TTS 引擎的真实门槛，给出 可用 / 勉强 / 不可用 结论）
  3. 一键部署（幂等：已就绪的步骤自动跳过；支持演练模式）

检测结果缓存到 hw_cache.json，避免每次进页面都重跑 torch（要 5~15 秒）。
"""
from __future__ import annotations

import json
import os
import queue
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path

import psutil

BASE = Path(__file__).resolve().parent
HW_CACHE = BASE / "hw_cache.json"

CREATE_NO_WINDOW = 0x08000000
_PROXY_KEYS = ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY", "all_proxy", "ALL_PROXY")
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

HW_TTL = 600          # 硬件缓存有效期（秒）
GB = 1024 ** 3

DEFAULT_REQ = {
    "vram_min_gb": 8.0,
    "vram_rec_gb": 12.0,
    "vram_fast_gb": 24.0,
    "cc_min": "7.5",
    "driver_min": "525.60",
    "ram_min_gb": 14.0,
    "ram_rec_gb": 32.0,
    "disk_min_gb": 25.0,
}


def _now() -> str:
    return datetime.now().strftime("%H:%M:%S")


# ------------------------------------------------------------------ 进程执行

def _clean_env() -> dict:
    env = os.environ.copy()
    for k in _PROXY_KEYS:
        env.pop(k, None)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["PIP_DEFAULT_TIMEOUT"] = "300"
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    return env


def _split_lines(chunk: str) -> list[str]:
    """pip 进度条用 \\r 刷新，按 \\r 和 \\n 一起切。"""
    return [x for x in re.split(r"[\r\n]+", chunk) if x.strip()]


def exec_stream(cmd, cwd=None, timeout=1800, emit=None, env_extra=None, max_lines=4000):
    """
    执行命令并实时回调输出，返回 (returncode, 汇总输出文本)。
    emit(text, level) 每行调用一次。
    """
    env = _clean_env()
    if env_extra:
        env.update(env_extra)
    try:
        p = subprocess.Popen(
            cmd, cwd=str(cwd) if cwd else None,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW, env=env, bufsize=0,
        )
    except FileNotFoundError:
        return 127, f"找不到可执行文件：{cmd[0]}"
    except OSError as e:
        return 1, f"启动失败：{e}"

    q: "queue.Queue[bytes | None]" = queue.Queue()

    def reader():
        try:
            for chunk in iter(lambda: p.stdout.read(4096), b""):
                q.put(chunk)
        except Exception:  # noqa: BLE001
            pass
        finally:
            q.put(None)

    threading.Thread(target=reader, daemon=True).start()

    buf = ""
    lines: list[str] = []
    t_end = time.time() + timeout
    killed = False
    while True:
        remain = t_end - time.time()
        if remain <= 0:
            killed = True
            try:
                p.kill()
            except Exception:  # noqa: BLE001
                pass
            break
        try:
            chunk = q.get(timeout=min(remain, 1.0))
        except queue.Empty:
            if p.poll() is not None and q.empty():
                break
            continue
        if chunk is None:
            break
        buf += chunk.decode("utf-8", "replace")
        parts = _split_lines(buf)
        if buf and not buf.endswith(("\n", "\r")):
            buf = parts.pop() if parts else buf
        else:
            buf = ""
        for line in parts:
            if len(lines) < max_lines:
                lines.append(line)
            if emit:
                emit(line)

    try:
        p.wait(timeout=5)
    except Exception:  # noqa: BLE001
        pass

    out = "\n".join(lines)
    if killed:
        return 124, out + f"\n[超时 {timeout}s，已终止]"
    return (p.returncode if p.returncode is not None else 1), out


def run_quiet(cmd, timeout=20, cwd=None):
    code, out = exec_stream(cmd, cwd=cwd, timeout=timeout)
    return code, out


# ------------------------------------------------------------------ 硬件检测

_TORCH_PROBE = """
import json
d = {"ok": False}
try:
    import torch
    d["ok"] = True
    d["version"] = torch.__version__
    d["cuda"] = torch.version.cuda
    d["available"] = bool(torch.cuda.is_available())
    try:
        d["arch"] = list(torch.cuda.get_arch_list())
    except Exception:
        d["arch"] = []
    if d["available"]:
        p = torch.cuda.get_device_properties(0)
        d["device"] = p.name
        d["vram_gb"] = round(p.total_memory / 1024 ** 3, 2)
        d["cc"] = "%d.%d" % (p.major, p.minor)
        try:
            d["bf16"] = bool(torch.cuda.is_bf16_supported())
        except Exception:
            d["bf16"] = None
except Exception as e:
    d["error"] = "%s: %s" % (type(e).__name__, e)
print("@@TORCH@@" + json.dumps(d))
"""


def _nvidia_smi() -> str | None:
    w = shutil.which("nvidia-smi")
    if w:
        return w
    for c in (r"C:\Windows\System32\nvidia-smi.exe",
              r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe"):
        if Path(c).exists():
            return c
    return None


def detect_gpu() -> list[dict]:
    exe = _nvidia_smi()
    if not exe:
        return []
    fields = "index,name,memory.total,memory.free,driver_version,compute_cap"
    code, out = run_quiet([exe, f"--query-gpu={fields}", "--format=csv,noheader,nounits"], timeout=15)
    if code != 0 or "not" in out.lower()[:40]:
        # 老驱动不支持 compute_cap，退一步
        code, out = run_quiet(
            [exe, "--query-gpu=index,name,memory.total,memory.free,driver_version",
             "--format=csv,noheader,nounits"], timeout=15)
        if code != 0:
            return []
        gpus = []
        for line in out.strip().splitlines():
            v = [x.strip() for x in line.split(",")]
            if len(v) >= 5:
                try:
                    gpus.append({"index": int(v[0]), "name": v[1],
                                 "vram_gb": round(float(v[2]) / 1024, 2),
                                 "free_gb": round(float(v[3]) / 1024, 2),
                                 "driver": v[4], "cc": None})
                except ValueError:
                    continue
        return gpus
    gpus = []
    for line in out.strip().splitlines():
        v = [x.strip() for x in line.split(",")]
        if len(v) >= 6:
            try:
                gpus.append({"index": int(v[0]), "name": v[1],
                             "vram_gb": round(float(v[2]) / 1024, 2),
                             "free_gb": round(float(v[3]) / 1024, 2),
                             "driver": v[4], "cc": v[5]})
            except ValueError:
                continue
    return gpus


def detect_torch(py: Path) -> dict:
    if not py or not Path(py).exists():
        return {"ok": False, "error": "虚拟环境里没有 python"}
    code, out = run_quiet([str(py), "-X", "utf8", "-c", _TORCH_PROBE], timeout=120)
    for line in out.splitlines():
        if line.startswith("@@TORCH@@"):
            try:
                return json.loads(line[len("@@TORCH@@"):])
            except json.JSONDecodeError:
                break
    return {"ok": False, "error": (out.strip()[-300:] or f"退出码 {code}")}


def disk_of(p: Path) -> dict:
    try:
        d = Path(p).drive or "C:"
        u = psutil.disk_usage(d + "\\")
        return {"drive": d, "total_gb": round(u.total / GB, 1),
                "free_gb": round(u.free / GB, 1), "used_pct": u.percent}
    except Exception:  # noqa: BLE001
        return {}


def detect_hardware(cfg: dict) -> dict:
    dep = cfg.get("deploy") or {}
    venv_py = Path(dep.get("venv_dir", "")) / "Scripts" / "python.exe" if dep.get("venv_dir") else None
    base_dir = Path(dep.get("base_dir") or "C:\\")

    mem = psutil.virtual_memory()
    cpu = {"name": "未知", "cores": os.cpu_count() or 0, "threads": os.cpu_count() or 0}
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                           r"HARDWARE\DESCRIPTION\System\CentralProcessor\0")
        cpu["name"] = winreg.QueryValueEx(k, "ProcessorNameString")[0].strip()
        winreg.CloseKey(k)
    except Exception:  # noqa: BLE001
        pass
    cpu["cores"] = psutil.cpu_count(logical=False) or cpu["cores"]
    cpu["threads"] = psutil.cpu_count(logical=True) or cpu["threads"]

    return {
        "ts": int(time.time()),
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "os": {"name": os.name, "release": _os_release()},
        "cpu": cpu,
        "ram": {"total_gb": round(mem.total / GB, 2), "avail_gb": round(mem.available / GB, 2),
                "used_pct": mem.percent},
        "disk": disk_of(base_dir),
        "gpu": detect_gpu(),
        "torch": detect_torch(venv_py) if venv_py else {"ok": False, "error": "未配置 venv 路径"},
        "python": {"venv": str(venv_py) if venv_py else None,
                   "venv_exists": bool(venv_py and Path(venv_py).exists())},
        "smi": _nvidia_smi(),
    }


def _os_release() -> str:
    try:
        import platform
        return f"{platform.system()} {platform.release()} ({platform.version()})"
    except Exception:  # noqa: BLE001
        return "unknown"


_hw_lock = threading.Lock()
_hw_mem: dict = {"ts": 0, "data": None}


def hardware(cfg: dict, force: bool = False) -> dict:
    with _hw_lock:
        cached = _hw_mem.get("data")
        fresh = cached and (time.time() - _hw_mem.get("ts", 0) < HW_TTL)
        if not force and fresh:
            return cached
    if not force:
        # 磁盘缓存兜底（面板重启后不用重跑 torch）
        try:
            raw = json.loads(HW_CACHE.read_text(encoding="utf-8"))
            if time.time() - raw.get("ts", 0) < HW_TTL and raw.get("data"):
                with _hw_lock:
                    _hw_mem["data"] = raw["data"]
                    _hw_mem["ts"] = raw.get("ts", 0)
                return raw["data"]
        except Exception:  # noqa: BLE001
            pass

    data = detect_hardware(cfg)
    with _hw_lock:
        _hw_mem["data"] = data
        _hw_mem["ts"] = data["ts"]
    try:
        HW_CACHE.write_text(json.dumps({"ts": data["ts"], "data": data}, ensure_ascii=False),
                            encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    return data


# ------------------------------------------------------------------ 能力评估

def _vtuple(s: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", str(s))[:3])


def assess(hw: dict, req: dict | None = None) -> dict:
    """对照门槛逐项判定，返回 {level, verdict, items[], headroom}。"""
    r = dict(DEFAULT_REQ)
    r.update(req or {})
    items: list[dict] = []

    def add(key, label, actual, need, level, note=""):
        items.append({"key": key, "label": label, "actual": actual,
                      "need": need, "level": level, "note": note})

    gpus = hw.get("gpu") or []
    vram = 0.0
    cc = None
    if not gpus:
        add("gpu", "独立显卡", "未检测到 NVIDIA GPU",
            f"显存 ≥ {r['vram_min_gb']:g} GB", "block",
            "nvidia-smi 没找到设备。3.48B 模型走 CPU 要几分钟才出一句话，没有可用性。")
    else:
        g = gpus[0]
        vram = float(g.get("vram_gb") or 0)
        cc = g.get("cc")
        extra = f"，共 {len(gpus)} 张卡" if len(gpus) > 1 else ""
        add("gpu", "独立显卡", f"{g['name']}（{vram:g} GB）{extra}",
            f"显存 ≥ {r['vram_min_gb']:g} GB",
            "ok" if vram >= r["vram_min_gb"] else "block",
            "" if vram >= r["vram_min_gb"] else "显存不够，模型加载会直接 OOM。")

        # 显存分档。标称 12 GB 的卡实际报 11.94 GiB，所以留 0.5 GB 容差。
        tol = 0.5
        if vram >= r["vram_fast_gb"] - tol:
            lvl, note = "ok", "够开官方 fast path（flash-attn），RTF 可到 0.3~1.0。"
        elif vram >= r["vram_rec_gb"] - tol:
            lvl, note = "ok", "走 eager 路径约占用 7.7 GB，RTF≈2.6（生成 1 秒音频约需 2.6 秒）。"
        elif vram >= r["vram_min_gb"] - tol:
            lvl, note = "warn", "在最低线附近，长段落容易 OOM；建议把单段字数调小、批大小降到 4。"
        else:
            lvl, note = "block", ""

        add("vram", "显存档位", f"{vram:g} GB",
            f"最低 {r['vram_min_gb']:g} / 推荐 {r['vram_rec_gb']:g} / fast {r['vram_fast_gb']:g} GB",
            lvl, note)

        free = g.get("free_gb")
        if free is not None and vram:
            used = vram - float(free)
            if used >= 1.0:
                add("vram_free", "当前显存占用", f"已用 {used:.1f} GB，空闲 {float(free):.1f} GB",
                    "参考", "ok",
                    "占用的多半是正在跑的网关；重新部署时它会先退出再加载。" if used > 2 else "")

        if cc:
            ok_cc = _vtuple(cc) >= _vtuple(r["cc_min"])
            add("cc", "计算能力", f"sm {cc}", f"≥ {r['cc_min']}",
                "ok" if ok_cc else "block",
                "" if ok_cc else "低于 Turing(sm_75)，当前 torch cu128 不包含该架构的 kernel。")
        drv = g.get("driver")
        if drv:
            ok_d = _vtuple(drv) >= _vtuple(r["driver_min"])
            add("driver", "显卡驱动", drv, f"≥ {r['driver_min']}",
                "ok" if ok_d else "warn",
                "" if ok_d else "驱动偏旧，cu128 轮子可能加载失败，建议升级到最新 Game Ready / Studio 驱动。")

    # torch / CUDA
    t = hw.get("torch") or {}
    if not t.get("ok"):
        add("torch", "PyTorch", "未安装或不可用", "CUDA 版 torch",
            "warn", "首次部署会自动安装，这一步不阻塞。" + (f"（{t.get('error', '')[:120]}）" if t.get("error") else ""))
    elif not t.get("available"):
        add("torch", "PyTorch", f"{t.get('version')} / CUDA 不可用", "cuda.is_available() == True",
            "block", "装成了 CPU 版，或者驱动与 CUDA 版本不匹配。部署流程会重装 CUDA 版。")
    else:
        cuda_ok = _vtuple(t.get("cuda") or "0") >= _vtuple("12.0")
        add("torch", "PyTorch", f"{t.get('version')} · CUDA {t.get('cuda')} · {t.get('device')}",
            "CUDA 12.x 及以上", "ok" if cuda_ok else "warn",
            "" if cuda_ok else "CUDA 版本偏低。")
        arch = t.get("arch") or []
        if cc:
            want = "sm_" + cc.replace(".", "")
            if arch and want not in arch:
                add("arch", "算子架构", f"torch 编译进 {len(arch)} 种架构，不含 {want}",
                    f"需要 {want}", "block", "这张卡跑不了这个轮子，必须换对应 CUDA 版本的 torch。")
            elif arch:
                add("arch", "算子架构", f"含 {want}", f"需要 {want}", "ok", "")
        if t.get("bf16") is False:
            add("bf16", "bfloat16", "不支持", "建议支持", "warn", "会回落到 fp16，显存占用略高。")

    # 内存
    ram = hw.get("ram") or {}
    total = float(ram.get("total_gb") or 0)
    if total:
        if total >= r["ram_min_gb"]:
            lvl = "ok"
            note = "模型权重会先预载进内存再灌进显存，够用。" if total < r["ram_rec_gb"] else ""
        else:
            lvl = "warn"
            note = "内存偏低，加载权重时可能被系统换页，启动会明显变慢。"
        add("ram", "系统内存", f"{total:g} GB（可用 {ram.get('avail_gb', '?')} GB）",
            f"≥ {r['ram_min_gb']:g} GB", lvl, note)

    # 磁盘
    d = hw.get("disk") or {}
    if d.get("free_gb") is not None:
        free = float(d["free_gb"])
        add("disk", "目标磁盘", f"{d['drive']} 剩余 {free:g} GB",
            f"≥ {r['disk_min_gb']:g} GB",
            "ok" if free >= r["disk_min_gb"] else "block",
            "模型权重 7.2 GB + 虚拟环境 5 GB + 余量。" if free >= r["disk_min_gb"] else "空间不够放权重和虚拟环境。")

    levels = [i["level"] for i in items]
    if "block" in levels:
        level = "block"
        verdict = "这台机器跑不了，先解决下面标红的问题。"
    elif "warn" in levels:
        level = "warn"
        verdict = "可以部署，但有需要注意的地方。"
    else:
        level = "ok"
        verdict = "硬件满足要求，可以一键部署。"

    fast = vram >= r["vram_fast_gb"]
    return {"level": level, "verdict": verdict, "items": items,
            "fast_path": fast, "vram_gb": vram, "req": r}


# ------------------------------------------------------------------ 部署步骤

def _gw_key_file(dep: dict) -> Path:
    return Path(dep.get("base_dir") or "") / "api_key.txt"


def _read_key(p: Path) -> str | None:
    try:
        s = p.read_text(encoding="utf-8").strip()
        return s or None
    except Exception:  # noqa: BLE001
        return None


def _model_state(model_dir: Path) -> tuple[bool, str]:
    if not model_dir.exists():
        return False, "模型目录不存在"
    files = [f for f in model_dir.rglob("*") if f.is_file()]
    total = sum(f.stat().st_size for f in files)
    st = [f for f in files if f.suffix == ".safetensors"]
    if not st:
        return False, f"目录里有 {len(files)} 个文件，但没有 safetensors 权重"
    have_index = any(f.name.endswith(".index.json") for f in files)
    gb = total / GB
    if gb < 6.5:
        return False, f"权重只有 {gb:.2f} GB，疑似下载不完整（完整约 7.2 GB）"
    if not have_index and gb < 7.5:
        return False, "缺少 model.safetensors.index.json"
    return True, f"{len(files)} 个文件 / {gb:.2f} GB"


def _health(port: int, key: str | None, timeout=4) -> tuple[bool, str]:
    req = urllib.request.Request(f"http://127.0.0.1:{port}/health")
    if key:
        req.add_header("X-API-Key", key)
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
            return True, f"status={d.get('status')}，采样率 {d.get('sample_rate')}"
    except urllib.error.HTTPError as e:
        if e.code == 503:
            return False, "服务已起，模型还在加载（503）"
        if e.code == 401:
            return False, "服务已起，但 API Key 不匹配（401）"
        return False, f"HTTP {e.code}"
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


class Step:
    def __init__(self, sid, name, desc, check, run=None, plan=None, manual=False, optional=False):
        self.id = sid
        self.name = name
        self.desc = desc
        self.check = check
        self.run = run
        self.plan = plan or desc
        self.manual = manual
        self.optional = optional

    def as_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "desc": self.desc, "plan": self.plan,
                "manual": self.manual, "optional": self.optional}


def build_task_script(py: Path, proj: Path, task: str) -> str:
    """生成注册计划任务的 PowerShell 脚本（纯 ASCII 注释，避免 PS5.1 的 CP936 坑）。"""
    return (
        "$ErrorActionPreference='Stop'\n"
        "$trg=@((New-ScheduledTaskTrigger -AtStartup),"
        "(New-ScheduledTaskTrigger -AtLogOn -User \"$env:USERDOMAIN\\$env:USERNAME\"))\n"
        "$set=New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries "
        "-StartWhenAvailable -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) "
        "-ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew\n"
        f"$act=New-ScheduledTaskAction -Execute '{py}' "
        f"-Argument '-X utf8 \"{proj}\\legado_gateway.py\"' -WorkingDirectory '{proj}'\n"
        "$prn=New-ScheduledTaskPrincipal -UserId \"$env:USERDOMAIN\\$env:USERNAME\" "
        "-LogonType Interactive -RunLevel Highest\n"
        f"Register-ScheduledTask -TaskName '{task}' -Action $act -Trigger $trg -Settings $set "
        "-Principal $prn -Force | Out-Null\n"
        f"if (Get-ScheduledTask -TaskName '{task}' -ErrorAction SilentlyContinue) "
        f"{{ Write-Output 'registered {task}' }} else {{ Write-Error 'register failed'; exit 3 }}\n"
    )


def build_steps(cfg: dict, hw: dict) -> list[Step]:
    dep = cfg.get("deploy") or {}
    base = Path(dep.get("base_dir") or (BASE.parent / "breeze-tts"))
    proj = Path(dep.get("project_dir") or base / "breeze-tts-main")
    venv = Path(dep.get("venv_dir") or base / "venv")
    model = Path(dep.get("model_dir") or base / "model")
    wheels = Path(dep.get("wheel_dir") or base / "wheels")
    port = int(dep.get("gateway_port") or 7861)
    py = venv / "Scripts" / "python.exe"
    key_file = _gw_key_file(dep)
    task = (cfg.get("deploy") or {}).get("task_name") or "BreezeTTSGateway"
    repo_url = dep.get("repo_url") or ""
    pip_idx = dep.get("pip_index") or "https://pypi.org/simple"
    torch_idx = dep.get("torch_index") or "https://download.pytorch.org/whl/cu128"
    base_py = dep.get("base_python") or "python"
    impl = dep.get("torch_version") or "2.9.1"

    steps: list[Step] = []

    # 1 代码目录
    def c_dir():
        gw = proj / "legado_gateway.py"
        if gw.exists():
            return True, f"{proj}"
        return False, f"缺少 {gw}"

    def r_dir(emit):
        emit(f"代码目录 {proj} 不完整，请先把仓库放到这个位置", "err")
        if repo_url:
            emit(f"仓库地址：{repo_url}（也可在 deploy.repo_url 里改）", "dim")
        return False, "代码缺失，无法自动下载"

    steps.append(Step("code", "代码目录",
                      "检查推理代码与网关入口是否就位",
                      c_dir, r_dir, "确认推理仓库已解压到项目目录"))

    # 2 虚拟环境
    def c_venv():
        if py.exists():
            code, out = run_quiet([str(py), "-c", "import sys;print(sys.version.split()[0])"], timeout=25)
            v = out.strip().splitlines()[-1] if out.strip() else "?"
            if code == 0:
                return True, f"Python {v}"
        return False, "虚拟环境未创建"

    def r_venv(emit):
        emit(f"创建虚拟环境：{venv}")
        venv.parent.mkdir(parents=True, exist_ok=True)
        code, out = exec_stream([base_py, "-m", "venv", str(venv)],
                                timeout=300, emit=lambda s: emit(s, "pipe"))
        if code != 0 or not py.exists():
            return False, f"创建失败（退出码 {code}）：{out[-300:]}"
        return True, "虚拟环境已创建"

    steps.append(Step("venv", "Python 虚拟环境",
                      "隔离依赖，避免污染系统 Python",
                      c_venv, r_venv, f"{base_py} -m venv {venv}"))

    # 3 torch
    def c_torch():
        t = hw.get("torch") or {}
        if t.get("ok") and t.get("available"):
            return True, f"torch {t.get('version')} · CUDA {t.get('cuda')} · {t.get('device')}"
        if t.get("ok"):
            return False, f"torch {t.get('version')} 已装，但 CUDA 不可用"
        return False, "torch 未安装"

    def r_torch(emit):
        local = sorted(wheels.glob("torch-*.whl")) + sorted(wheels.glob("torchaudio-*.whl")) \
            if wheels.exists() else []
        if local:
            emit(f"发现本地 wheel {len(local)} 个，离线安装", "info")
            code, out = exec_stream(
                [str(py), "-m", "pip", "install", "--no-deps", "--force-reinstall", *map(str, local)],
                timeout=1800, emit=lambda s: emit(s, "pipe"))
        else:
            emit(f"从 {torch_idx} 安装 torch {impl}", "info")
            code, out = exec_stream(
                [str(py), "-m", "pip", "install", f"torch=={impl}", f"torchaudio=={impl}",
                 "--index-url", torch_idx, "--timeout", "300", "--retries", "10"],
                timeout=5400, emit=lambda s: emit(s, "pipe"))
        if code != 0:
            return False, f"安装失败（退出码 {code}）：{out[-400:]}"
        t = detect_torch(py)
        if not t.get("available"):
            return False, f"装完仍不可用：{t.get('error') or t}"
        return True, f"torch {t.get('version')} · CUDA {t.get('cuda')}"

    steps.append(Step("torch", "PyTorch (CUDA)",
                      "cu128 轮子；装了 CPU 版会导致 CUDA 直接消失",
                      c_torch, r_torch,
                      f"pip install torch=={impl} --index-url {torch_idx}"))

    # 4 依赖
    dep_mods = ["fastapi", "uvicorn", "soundfile", "numpy", "transformers", "lameenc"]
    _SPEC_CHECK = (
        "import importlib.util as u\n"
        f"mods={dep_mods!r}\n"
        "miss=[m for m in mods if u.find_spec(m) is None]\n"
        "print('MISSING:'+','.join(miss) if miss else 'ALL-OK')\n"
    )

    def c_deps():
        if not py.exists():
            return False, "虚拟环境还没建好"
        # find_spec 只查包是否存在，不真正 import，比 import 快一个数量级
        code, out = run_quiet([str(py), "-c", _SPEC_CHECK], timeout=60)
        if "ALL-OK" in out:
            return True, "、".join(dep_mods) + " 均已安装"
        m = re.search(r"MISSING:([\w,\-]+)", out)
        if m:
            return False, "缺少 " + "、".join(m.group(1).split(","))
        return False, f"检查失败：{out.strip()[-200:]}"

    def r_deps(emit):
        req = proj / "requirements.txt"
        if req.exists():
            emit("安装 requirements.txt（torch 已就位会被跳过）", "info")
            code, out = exec_stream([str(py), "-m", "pip", "install", "-r", str(req),
                                     "--index-url", pip_idx, "--timeout", "300", "--retries", "10"],
                                    timeout=3600, emit=lambda s: emit(s, "pipe"))
            if code != 0:
                return False, f"安装失败（退出码 {code}）：{out[-400:]}"
        emit("补装网关专用依赖 lameenc（MP3 编码）", "info")
        code, out = exec_stream([str(py), "-m", "pip", "install", "lameenc",
                                 "--index-url", pip_idx, "--timeout", "300"],
                                timeout=600, emit=lambda s: emit(s, "pipe"))
        if code != 0:
            return False, f"lameenc 安装失败：{out[-300:]}"

        # gradio 会把 cu128 torch 覆盖成 CPU 版，装完必须回归校验
        t = detect_torch(py)
        if not t.get("available"):
            emit("检测到 torch 的 CUDA 支持丢失（多半是 gradio 覆盖），重装 CUDA 版", "warn")
            local = sorted(wheels.glob("torch*.whl")) if wheels.exists() else []
            if local:
                exec_stream([str(py), "-m", "pip", "install", "--no-deps", "--force-reinstall",
                             *map(str, local)], timeout=1800, emit=lambda s: emit(s, "pipe"))
            else:
                exec_stream([str(py), "-m", "pip", "install", "--force-reinstall", "--no-deps",
                             f"torch=={impl}", f"torchaudio=={impl}",
                             "--index-url", torch_idx], timeout=3600, emit=lambda s: emit(s, "pipe"))
            t = detect_torch(py)
            if not t.get("available"):
                return False, "torch CUDA 仍不可用，需要手工排查"
        code, out = run_quiet([str(py), "-c", "import " + ",".join(dep_mods) + ";print('ok')"], timeout=90)
        if code != 0:
            return False, f"依赖仍不完整：{out[-300:]}"
        return True, "依赖齐全"

    steps.append(Step("deps", "Python 依赖",
                      "FastAPI / uvicorn / soundfile / transformers / lameenc",
                      c_deps, r_deps,
                      f"pip install -r requirements.txt（镜像 {pip_idx}）"))

    # 5 模型权重
    def c_model():
        return _model_state(model)

    def r_model(emit):
        ok, detail = _model_state(model)
        if ok:
            return True, detail
        dl = base / "dl_breeze.ps1"
        emit(f"权重缺失：{detail}", "err")
        emit("权重约 7.2 GB，不适合在面板里同步下载（会阻塞很久且易断）。", "warn")
        if dl.exists():
            emit(f"手动执行：powershell -NoProfile -ExecutionPolicy Bypass -File \"{dl}\"", "info")
            emit("下完再点一次「一键部署」，会从这一步继续。", "info")
        else:
            emit("从 ModelScope 下载 BreezeBlue/Breeze-TTS-2 放到模型目录。", "info")
        return False, f"需要手动准备权重（{detail}）"

    steps.append(Step("model", "模型权重",
                      "7.2 GB safetensors + audio_tokenizer",
                      c_model, r_model,
                      f"校验 {model} 下的 safetensors 完整性（缺失则需手动下载）"))

    # 6 API Key
    def c_key():
        k = _read_key(key_file)
        if k:
            return True, f"{key_file.name} 已存在（{k[:6]}…）"
        return False, "API Key 未生成"

    def r_key(emit):
        import secrets
        key_file.parent.mkdir(parents=True, exist_ok=True)
        k = secrets.token_urlsafe(24)[:32]
        key_file.write_text(k + "\n", encoding="utf-8")
        emit(f"已生成 API Key 写入 {key_file}", "ok")
        return True, "API Key 已生成"

    steps.append(Step("key", "API Key",
                      "网关鉴权用，没有它服务相当于裸奔",
                      c_key, r_key, f"生成随机 Key 写入 {key_file}"))

    # 7 计划任务
    def c_task():
        code, out = run_quiet(
            ["powershell.exe", "-NoProfile", "-Command",
             f"$t=Get-ScheduledTask -TaskName '{task}' -ErrorAction SilentlyContinue;"
             f"if($t){{$t.State}}else{{'missing'}}"], timeout=40)
        s = out.strip().splitlines()[-1].strip() if out.strip() else "missing"
        if s.lower() == "missing":
            return False, f"计划任务 {task} 不存在"
        if s.lower() == "disabled":
            return False, f"计划任务 {task} 已禁用"
        return True, f"计划任务 {task}（{s}）"

    def r_task(emit):
        script = build_task_script(py, proj, task)
        tmp = BASE / f"_deploy_task_{task}.ps1"
        tmp.write_text(script, encoding="utf-8")
        emit("注册开机自启计划任务", "info")
        code, out = exec_stream(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(tmp)],
            timeout=120, emit=lambda s: emit(s, "pipe"))
        if code != 0 or "register failed" in out:
            return False, f"注册失败：{out[-300:]}"
        return True, f"计划任务 {task} 已注册"

    steps.append(Step("task", "开机自启",
                      "计划任务形式常驻，崩溃自动重启",
                      c_task, r_task, f"注册计划任务 {task}（AtStartup + AtLogOn）"))

    # 8 启动并等待就绪
    key = _read_key(key_file)

    def c_start():
        ok, detail = _health(port, key)
        return ok, detail

    def r_start(emit):
        emit(f"启动计划任务 {task}", "info")
        exec_stream(["powershell.exe", "-NoProfile", "-Command",
                     f"Start-ScheduledTask -TaskName '{task}'"], timeout=60,
                    emit=lambda s: emit(s, "pipe"))
        emit("等待 /health 就绪（首次加载模型约 40~90 秒）", "info")
        t0 = time.time()
        last = ""
        while time.time() - t0 < 300:
            time.sleep(5)
            ok, detail = _health(port, key)
            if ok:
                return True, f"已就绪，耗时 {int(time.time() - t0)} 秒"
            if detail != last:
                emit(f"  {detail}", "dim")
                last = detail
            else:
                emit(f"  仍在等待… {int(time.time() - t0)}s", "dim")
        return False, "300 秒内未就绪，看网关日志排查"

    steps.append(Step("start", "启动服务",
                      "触发计划任务并轮询 /health 直到就绪",
                      c_start, r_start, f"Start-ScheduledTask {task} 后轮询 http://127.0.0.1:{port}/health"))

    # 9 端到端自检（每次都跑）
    def c_selftest():
        return False, "自检每次都执行"

    def r_selftest(emit):
        url = f"http://127.0.0.1:{port}/tts"
        req = urllib.request.Request(url, data="部署完成，这是自检语音。".encode("utf-8"),
                                     method="POST")
        req.add_header("Content-Type", "text/plain; charset=utf-8")
        if key:
            req.add_header("X-API-Key", key)
        emit("合成一句测试音频…", "info")
        t0 = time.time()
        try:
            with _OPENER.open(req, timeout=180) as r:
                data = r.read()
                ct = r.headers.get("Content-Type", "")
        except Exception as e:  # noqa: BLE001
            return False, f"合成失败：{type(e).__name__}: {e}"
        el = time.time() - t0
        if not data:
            return False, "返回空音频"
        head = data[:3]
        is_mp3 = head[:2] == b"\xff\xfb" or head[:2] == b"\xff\xf3" or head[:3] == b"ID3"
        if "mpeg" in ct and not is_mp3:
            return False, f"返回了 {len(data)} 字节，但 MP3 帧头不对（{head.hex()}）"
        emit(f"收到 {len(data) / 1024:.1f} KB，帧头 {head.hex()}，耗时 {el:.1f}s", "ok")
        return True, f"{len(data) / 1024:.1f} KB / {el:.1f}s"

    steps.append(Step("selftest", "端到端自检",
                      "真的合成一句话，校验 MP3 帧头",
                      c_selftest, r_selftest, "POST /tts 合成测试音频并校验帧头"))

    return steps


def plan(cfg: dict, hw: dict) -> list[dict]:
    steps = build_steps(cfg, hw)
    out = []
    for s in steps:
        try:
            ok, detail = s.check()
        except Exception as e:  # noqa: BLE001
            ok, detail = False, f"检查异常：{type(e).__name__}: {e}"
        d = s.as_dict()
        d["ready"] = bool(ok)
        d["detail"] = detail
        out.append(d)
    return out


# ------------------------------------------------------------------ 任务执行

class Job:
    def __init__(self, dry: bool):
        self.id = uuid.uuid4().hex[:10]
        self.dry = dry
        self.state = "running"      # running / ok / fail / canceled / manual
        self.created = time.time()
        self.finished = None
        self.lines: list[dict] = []
        self.steps: list[dict] = []
        self.error = None
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self.emit("开始" + ("演练（不实际执行）" if dry else "一键部署"), "head")

    def emit(self, text: str, level="info", step: str | None = None) -> None:
        with self._lock:
            self.lines.append({"i": len(self.lines), "t": _now(),
                               "level": level, "step": step, "text": text[:2000]})
            if len(self.lines) > 4000:
                del self.lines[:1000]

    def poll(self, since: int = 0) -> dict:
        with self._lock:
            lines = self.lines[since:][:600]
            return {
                "id": self.id, "state": self.state, "dry": self.dry,
                "steps": list(self.steps), "lines": lines,
                "cursor": since + len(lines), "total": len(self.lines),
                "error": self.error,
                "elapsed": int((self.finished or time.time()) - self.created),
            }

    def step(self, sid: str, state: str, detail: str = "") -> None:
        with self._lock:
            for s in self.steps:
                if s["id"] == sid:
                    s["state"] = state
                    if detail:
                        s["detail"] = detail
                    if state in ("ok", "skip", "fail", "manual", "dry"):
                        s["ended"] = int(time.time() - self.created)
                    else:
                        s["started"] = int(time.time() - self.created)
                    return

    def cancel(self) -> None:
        self._cancel.set()


JOBS: dict[str, Job] = {}
_job_lock = threading.Lock()


def start_job(cfg: dict, dry: bool) -> Job:
    hw = hardware(cfg)
    job = Job(dry)
    with _job_lock:
        JOBS[job.id] = job
        # 只保留最近 20 个
        if len(JOBS) > 20:
            for k in sorted(JOBS, key=lambda k: JOBS[k].created)[:-20]:
                JOBS.pop(k, None)
    threading.Thread(target=_run_job, args=(job, cfg, hw, dry), daemon=True).start()
    return job


def _run_job(job: Job, cfg: dict, hw: dict, dry: bool) -> None:
    try:
        a = assess(hw, (cfg.get("deploy") or {}).get("requirements"))
        job.emit(f"硬件结论：{a['verdict']}（{a['level']}）", "head")
        if a["level"] == "block" and not dry:
            for it in a["items"]:
                if it["level"] == "block":
                    job.emit(f"  阻塞项 · {it['label']}：{it['actual']} / 需要 {it['need']}", "err")
            job.state = "fail"
            job.error = "硬件不满足要求，已中止。可以先用演练模式看看会执行哪些步骤。"
            job.finished = time.time()
            job.emit(job.error, "err")
            return

        steps = build_steps(cfg, hw)
        job.steps = [{"id": s.id, "name": s.name, "desc": s.desc,
                      "state": "pending", "detail": ""} for s in steps]

        for s in steps:
            if job._cancel.is_set():
                job.state = "canceled"
                job.emit("已取消", "warn")
                job.finished = time.time()
                return
            job.step(s.id, "running")
            job.emit(f"▸ {s.name} — {s.desc}", "head", s.id)
            try:
                ok, detail = s.check()
            except Exception as e:  # noqa: BLE001
                ok, detail = False, f"检查异常：{type(e).__name__}: {e}"

            if ok and s.id != "selftest":
                job.step(s.id, "skip", detail)
                job.emit(f"  已就绪，跳过：{detail}", "ok", s.id)
                continue

            if dry:
                job.step(s.id, "dry", f"将执行：{s.plan}")
                job.emit(f"  将执行：{s.plan}", "dim", s.id)
                if not ok:
                    job.emit(f"  当前状态：{detail}", "dim", s.id)
                continue

            job.emit(f"  当前：{detail}", "dim", s.id)
            if s.manual:
                job.step(s.id, "manual", detail)
                job.state = "manual"
                job.error = f"「{s.name}」需要人工介入：{detail}"
                job.emit(f"需要人工介入：{detail}", "err", s.id)
                job.finished = time.time()
                return

            def emit_line(text, level="pipe", _sid=s.id):
                if level == "pipe":
                    low = text.lower()
                    if any(w in low for w in ("error", "failed", "traceback", "拒绝", "失败")):
                        level = "err"
                    elif "warning" in low:
                        level = "warn"
                    else:
                        level = "dim"
                job.emit(text, level, _sid)

            try:
                ok2, msg = s.run(emit_line)
            except Exception as e:  # noqa: BLE001
                ok2, msg = False, f"{type(e).__name__}: {e}"
            if not ok2:
                job.step(s.id, "fail", msg)
                job.state = "fail"
                job.error = f"「{s.name}」失败：{msg}"
                job.emit(job.error, "err", s.id)
                job.finished = time.time()
                return
            job.step(s.id, "ok", msg)
            job.emit(f"  ✓ {msg}", "ok", s.id)

        # 收尾刷新硬件缓存（部署后 torch 状态变了）
        try:
            hardware(cfg, force=True)
        except Exception:  # noqa: BLE001
            pass

        job.state = "ok"
        job.emit("全部步骤完成，服务已就绪" if not dry else "演练结束（未做任何改动）", "head")
        job.finished = time.time()
    except Exception as e:  # noqa: BLE001
        import traceback
        job.state = "fail"
        job.error = f"{type(e).__name__}: {e}"
        job.emit(traceback.format_exc()[-1200:], "err")
        job.finished = time.time()


def get_job(jid: str, since: int = 0) -> dict | None:
    j = JOBS.get(jid)
    return j.poll(since) if j else None


def running_job() -> dict | None:
    for j in JOBS.values():
        if j.state == "running":
            return {"id": j.id, "dry": j.dry, "elapsed": int(time.time() - j.created)}
    return None
