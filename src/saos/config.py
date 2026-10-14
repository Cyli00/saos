"""运行时路径与常量。所有位置都由 SAOS_HOME 推出，测试时改 SAOS_HOME 即可完全隔离。"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

DEFAULT_OPTICSTUDIO_DIR = r"C:\Program Files\Ansys Zemax OpticStudio 2024 R1.00"
# DESIGN.md 锁定 OpticStudio 2024 R1.00
EXPECTED_ZOS_VERSION = (24, 1, 0)


WORK_ROOT = REPO_ROOT / "work"
DEFAULT_INSTANCE = "main"
INSTANCE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,47}$")


def saos_home() -> Path:
    """运行目录的优先级：SAOS_HOME（任意路径，测试用）> SAOS_INSTANCE（落在 work/<实例名>/）> work/main/。"""
    explicit = os.environ.get("SAOS_HOME")
    if explicit:
        return Path(explicit).resolve()
    instance = os.environ.get("SAOS_INSTANCE") or DEFAULT_INSTANCE
    if not INSTANCE_RE.match(instance):
        from saos.protocol import ErrorCode, SaosError

        raise SaosError(ErrorCode.BAD_ARGS, f"SAOS_INSTANCE: letters, digits, - and _, 1–48 chars, starting with a letter or digit: {instance!r}")
    return (WORK_ROOT / instance).resolve()


def jobs_dir() -> Path:
    return saos_home() / "jobs"


def daemon_dir() -> Path:
    return saos_home() / "daemon"


def daemon_state_file() -> Path:
    return daemon_dir() / "state.json"


def daemon_authkey_file() -> Path:
    return daemon_dir() / "authkey"


def daemon_log_file() -> Path:
    return daemon_dir() / "daemon.log"


def pipe_address() -> str:
    # 不同 SAOS_HOME 使用不同管道名，测试和正式环境的后台进程互不干扰
    digest = hashlib.sha1(str(saos_home()).lower().encode("utf-8")).hexdigest()[:12]
    return rf"\\.\pipe\saos-{digest}"


def opticstudio_dir() -> str:
    return os.environ.get("SAOS_OPTICSTUDIO_DIR", DEFAULT_OPTICSTUDIO_DIR)


def backend_name() -> str:
    return os.environ.get("SAOS_BACKEND", "zos")


def code_sha256() -> str:
    """saos 包全部 .py 源文件（按相对路径排序）的 SHA-256，用来确认实验各进程运行的是同一版本代码。"""
    import hashlib

    pkg = Path(__file__).resolve().parent
    h = hashlib.sha256()
    for f in sorted(pkg.rglob("*.py")):
        h.update(f.relative_to(pkg).as_posix().encode("utf-8") + b"\0" + f.read_bytes() + b"\0")
    return h.hexdigest()
