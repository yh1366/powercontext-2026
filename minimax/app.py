"""启动入口：``python app.py``（等价 ``python main.py`` / ``python -m sddctx serve``）。

默认监听 127.0.0.1:8000，可用 ``HOST`` / ``PORT`` 覆盖：

    cd minimax && pip install -r requirements.txt && python app.py
    curl "http://127.0.0.1:8000/api/context?task=TASK-0007&format=json" | head

命令行入口（推荐，评审脚本更好写）见 ``python -m sddctx --help``。
"""

from __future__ import annotations

import os

from sddctx.config import load_settings
from sddctx.http import serve


def main() -> None:
    settings = load_settings()
    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "8000"))
    serve(settings, host=host, port=port)


if __name__ == "__main__":
    main()
