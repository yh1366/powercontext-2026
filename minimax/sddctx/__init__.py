"""sddctx —— 面向软件持续演进的 SDD 上下文管理模块。

模块职责（对应题面 2.3）：

* ``ingest``  把规格、代码、测试资料导入 seekdb / OceanBase（或离线降级的 SQLite），建立检索索引；
* ``context`` 按开发任务挑选适用的规格条目、代码符号与测试，组装开发上下文包；
* ``state``   用 PowerContext 保存任务进展与验证结果，重启后读回并继续使用；
* ``verify``  运行目标测试与回归测试，把命令、退出码与结论写入状态。

所有入口都收敛在 :mod:`sddctx.cli`，HTTP 入口见 ``minimax/app.py``。
"""

__all__ = ["__version__"]

__version__ = "1.0.0"
