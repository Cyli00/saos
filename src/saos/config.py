"""运行时路径与常量。所有位置都由 SAOS_HOME 推出，测试时改 SAOS_HOME 即可完全隔离。"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_DIR.parents[1]
# 源码检出（仓库里 uv run）还是装好的包：装成包时 REPO_ROOT 指不到仓库，库、技能和运行目录改用包内副本与用户目录
# a source checkout (uv run in the repository) or an installed package, whose REPO_ROOT is no repository
IN_CHECKOUT = (REPO_ROOT / "pyproject.toml").is_file() and (REPO_ROOT / "src" / "saos").resolve() == PACKAGE_DIR
# 装成包时随包分发的共享库与技能（pyproject 的 force-include）/ the library and skill shipped inside the wheel
PACKAGED_LIBRARY = PACKAGE_DIR / "_library"
PACKAGED_SKILL = PACKAGE_DIR / "_skill"


def library_dir() -> Path:
    """共享参考库的位置：SAOS_LIBRARY > 源码检出的 library/ > 包内副本。
    The shared reference library: SAOS_LIBRARY, else the checkout's library/, else the copy inside the package."""
    explicit = os.environ.get("SAOS_LIBRARY")
    if explicit:
        return Path(explicit).resolve()
    return REPO_ROOT / "library" if IN_CHECKOUT else PACKAGED_LIBRARY


def skill_dir() -> Path:
    """saos 技能（SKILL.md 与 references/）的位置：源码检出里是 .agents/skills/saos，装成包时是包内副本。
    The saos skill: .agents/skills/saos in a checkout, the copy inside the package otherwise."""
    return REPO_ROOT / ".agents" / "skills" / "saos" if IN_CHECKOUT else PACKAGED_SKILL


def code_path(rel: str) -> Path:
    """判定记录里 saos 代码文件的标签（src/saos/<文件>，与旧记录一致）对应的实际文件；装成包时在包目录里。
    The file behind a check record's saos code label (src/saos/<file>, as older records name it), in the package."""
    prefix = "src/saos/"
    if not rel.startswith(prefix):
        raise ValueError(f"not a saos code label: {rel}")
    return PACKAGE_DIR / rel[len(prefix):]

DEFAULT_OPTICSTUDIO_DIR = r"C:\Program Files\Ansys Zemax OpticStudio 2024 R1.00"
# 锁定 OpticStudio 2024 R1.00
EXPECTED_ZOS_VERSION = (24, 1, 0)
# 许可证同时允许的 OpticStudio 实例数，用户手动打开的窗口也计在内（2026-10-15 实测）
LICENSED_INSTANCES = 8


# 计算时给用户留下的逻辑核数，其余都可以用 / logical cores left to the user during computation; the rest may be used
RESERVED_CORES = 2


def compute_cores() -> int:
    return max(1, (os.cpu_count() or 1) - RESERVED_CORES)


# 运行目录的根：源码检出里是仓库的 work/；装成包时落到用户目录，不写进 site-packages
# the root of runtime directories: the checkout's work/, or the user's home for an installed package
WORK_ROOT = REPO_ROOT / "work" if IN_CHECKOUT else Path.home() / ".saos" / "work"
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


def daemon_registry_dir() -> Path:
    """本机所有 daemon 的登记目录（与 SAOS_HOME 无关）：启动时写一条，退出时删除，daemon list/stop --all 据此找到
    work/ 之外（例如测试用临时 SAOS_HOME）启动的 daemon。SAOS_DAEMON_REGISTRY 可改位置。
    The machine-wide registry of daemons, independent of SAOS_HOME."""
    explicit = os.environ.get("SAOS_DAEMON_REGISTRY")
    if explicit:
        return Path(explicit)
    return Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "saos" / "daemons"


def pipe_address() -> str:
    # 不同 SAOS_HOME 使用不同管道名，测试和正式环境的后台进程互不干扰
    digest = hashlib.sha1(str(saos_home()).lower().encode("utf-8")).hexdigest()[:12]
    return rf"\\.\pipe\saos-{digest}"


def opticstudio_dir() -> str:
    return os.environ.get("SAOS_OPTICSTUDIO_DIR", DEFAULT_OPTICSTUDIO_DIR)


def backend_name() -> str:
    return os.environ.get("SAOS_BACKEND", "auto").strip().lower()


def glass_dir() -> Path | None:
    """rtc 实例读 AGF 玻璃目录的文件夹：SAOS_GLASSCAT，没设时为 None（rtc 自己的默认：RTC_GLASSCAT 或 Documents/Zemax/Glasscat）。
    The folder the rtc instance reads AGF catalogs from: SAOS_GLASSCAT, else None (rtc's own default)."""
    value = os.environ.get("SAOS_GLASSCAT", "").strip()
    return Path(value) if value else None


COMPUTE_PRECISION = "float64"


def code_sha256() -> str:
    """saos 包全部 .py 源文件（按相对路径排序）的 SHA-256，用来确认实验各进程运行的是同一版本代码。"""
    import hashlib

    pkg = Path(__file__).resolve().parent
    h = hashlib.sha256()
    for f in sorted(pkg.rglob("*.py")):
        h.update(f.relative_to(pkg).as_posix().encode("utf-8") + b"\0" + f.read_bytes() + b"\0")
    return h.hexdigest()
