# MiniMax 开发记录

本记录由 `scripts/render_dev_log.py` 从 `runs/` 下的落盘文件自动生成，
内容与提交的 `patches/` 补丁、`docs/logs/` 测试日志一一对应。

## 1. 接入方式

| 项目 | 值 |
| --- | --- |
| 接口 | `POST {base}/v1/text/chatcompletion_v2`（MiniMax 开放平台 chatcompletion_v2 协议） |
| 模型 | `MiniMax-M2` |
| 客户端 | `sddctx/minimax_client.py`，标准库 urllib，无 SDK |
| 温度 / 上限 | temperature=0.1，max_tokens=8000 |
| 产物 | `runs/<run_id>/minimax.request.json`、`minimax.response.json`、`codegen.json` |

## 2. 生成记录

> **待补充**：当前仓库尚未包含一次真实的生成记录（`MINIMAX_API_KEY` 申请中）。
> Key 就绪后执行下面的命令即可自动补齐本节：

```bash
cd minimax
python -m sddctx demo reset                      # 确保工作区处于修改前状态
python -m sddctx codegen --task TASK-0007 --apply # 调 MiniMax 出补丁并落地快照
python scripts/render_dev_log.py
```

在此之前，`patches/TASK-0007.reference.patch` 是人工撰写的参考实现，
仅用于打通「上下文 → 改代码 → 验证」链路，**不作为 MiniMax 参与的证据**。

## 附：如何复跑

```bash
cd minimax
export MINIMAX_API_KEY=...           # 或用 .env.local
python -m sddctx demo reset           # 回到修改前
python -m sddctx codegen --task TASK-0007 --apply
python scripts/render_dev_log.py
```
