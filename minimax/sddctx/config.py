"""运行配置：全部来自环境变量，仓库里不出现任何真实凭据。

支持 ``minimax/.env.local``（已加入 .gitignore），方便本地反复调试；
环境变量优先级高于该文件，文件里已有的键不会覆盖进程环境。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env.local"


def _load_env_file(path: Path) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _flag(name: str, default: str = "auto") -> str:
    return (os.getenv(name) or default).strip().lower()


@dataclass(frozen=True)
class Settings:
    """一次进程运行用到的全部配置。"""

    root: Path
    data_dir: Path
    db_backend: str
    seekdb_host: str
    seekdb_port: int
    seekdb_user: str
    seekdb_password: str
    seekdb_database: str
    state_backend: str
    powercontext_url: str
    powercontext_scope_id: str
    powercontext_scope_key: str
    minimax_api_key: str
    minimax_base_url: str
    minimax_model: str
    spec_variant: str

    @property
    def demo_project(self) -> Path:
        return self.root / "demo_project"

    @property
    def specs_dir(self) -> Path:
        return self.root / "specs"

    @property
    def tasks_dir(self) -> Path:
        return self.root / "tasks"

    @property
    def patches_dir(self) -> Path:
        return self.root / "patches"

    @property
    def snapshots_dir(self) -> Path:
        return self.root / "snapshots"

    @property
    def sqlite_path(self) -> Path:
        return self.data_dir / "sddctx.sqlite3"

    def task_path(self, task_id: str) -> Path:
        return self.tasks_dir / f"{task_id}.json"


def load_settings() -> Settings:
    _load_env_file(ENV_FILE)
    data_dir = Path(os.getenv("SDDCTX_HOME") or (ROOT / "runs")).resolve()
    return Settings(
        root=ROOT,
        data_dir=data_dir,
        db_backend=_flag("SDDCTX_DB", "auto"),
        seekdb_host=os.getenv("SEEKDB_HOST", "127.0.0.1"),
        seekdb_port=int(os.getenv("SEEKDB_PORT", "2881")),
        seekdb_user=os.getenv("SEEKDB_USER", "root"),
        seekdb_password=os.getenv("SEEKDB_PASSWORD", ""),
        seekdb_database=os.getenv("SEEKDB_DATABASE", "sddctx"),
        state_backend=_flag("SDDCTX_STATE", "auto"),
        powercontext_url=os.getenv("POWERCONTEXT_API_URL", "http://127.0.0.1:8848"),
        powercontext_scope_id=os.getenv("POWERCONTEXT_SCOPE_ID", ""),
        powercontext_scope_key=os.getenv("POWERCONTEXT_SCOPE_KEY", "sddctx-ordercore-tasks"),
        minimax_api_key=os.getenv("MINIMAX_API_KEY", ""),
        minimax_base_url=os.getenv("MINIMAX_BASE_URL", "https://api.minimaxi.com"),
        minimax_model=os.getenv("MINIMAX_MODEL", "MiniMax-M2.7"),
        spec_variant=os.getenv("SDDCTX_SPEC_VARIANT", "v1"),
    )
