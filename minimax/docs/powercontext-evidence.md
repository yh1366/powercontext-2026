# PowerContext 状态写入与复用记录

## 1. 接入方式

| 项目 | 值 |
| --- | --- |
| 版本 | PowerContext Server **1.4.1**（`pip install "powercontext[server]==1.4.1"`） |
| 启动 | `powercontext server run --host 127.0.0.1 --port 8848 --role all --no-env-file` |
| 接入 | HTTP/JSON，标准库 urllib（不引 SDK，避免版本耦合） |
| Scope | 由固定幂等键 `sddctx-ordercore-tasks` 创建/找回，本次运行得到 `scp_588xvxtd5x499bcd231s2b2793` |
| 记忆 kind | `sdd-task-state` |
| 记忆文本 | 单行 JSON，前缀 `[sddctx]`，例如 `[sddctx] {"task_id": "TASK-0007", "event": "verify", …}` |

实际发出的 3 类请求（响应状态码已核对）：

```
GET  /health/live                 → 200
POST /v1/scopes                   → 201  {"title": "...", "summary": "...", "idempotency_key": "sddctx-ordercore-tasks"}
POST /v1/memory/remember          → 200  {"scope_id": "scp_...", "kind": "sdd-task-state", "text": "[sddctx] {...}"}
POST /v1/memory/entries/list      → 200  读回全部事件（分页，含 next_cursor）
```

## 2. 写入的事件流

`python -m sddctx demo sample` 一次运行产生的完整事件流（时间顺序，共 9 条；开头还有一条
`state_reset` 标记，表示「从空状态开始记录」）：

| # | 时刻 | 事件 | 关键 payload |
| --- | --- | --- | --- |
| 1 | 21:19:36 | `verify` | `phase=before, exit_code=1, passed=false`，commit `bb14d55` |
| 2 | 21:19:42 | `context_built` | `run_id=run-…211941, spec_variant=v1`，backend `seekdb` |
| 3 | 21:19:42 | `step_done` | `step=S1`（确认规格口径） |
| 4 | 21:19:42 | `patch_applied` | `files=["demo_project/ordercore/refund.py"], patch="patches/TASK-0007.patch"` |
| 5 | 21:19:43 | `step_done` | `step=S2`（实现运费分摊） |
| 6 | 21:19:43 | `step_done` | `step=S3`（实现累计上限） |
| 7 | 21:19:49 | `verify` | `phase=after, exit_code=0, passed=true` |
| 8 | 21:19:50 | `step_done` | `step=S4`（运行测试并记录） |
| 9 | 21:19:56 | `context_built` | `spec_variant=v2`（规格演进演练） |

`demo sample` 开头会写一条 `state_reset`，所以事件数从 9 重新开始计数；
上一轮累积的 25 条事件仍在 Scope 里（见 [`logs/19_state_show.log`](logs/19_state_show.log)），
只是重置标记之后不再被折叠。

## 3. 复用：换一个进程读回来

状态不是进程内变量，也不是「打印过的日志」——每次 `sddctx` 命令都是**新进程**，
状态由 `/v1/memory/entries/list` 读回后折叠成任务视图：

```
$ python -m sddctx state show --task TASK-0007
状态后端：powercontext / scp_588xvxtd5x499bcd231s2b2793
事件数：25
已完成：
  - 确认 SPEC-003#3.2 运费分摊口径与 SPEC-003#3.4 累计上限口径
  - 在 RefundService.quote_partial_refund 中实现运费按商品金额占比分摊
  - 实现累计退款上限：可退余额 = 实付 - 已退，超额截断并返回告警
  - 运行目标测试与回归测试，记录退出码与结论
待办：
  - （无）
验证记录：
  - before：exit=1，passed=False …
  - after：exit=0，passed=True …
```

（原文见 [`logs/19_state_show.log`](logs/19_state_show.log)。）

状态的用途不只是展示：

1. **参与上下文**：`context` 输出的 `state` 段就是这份视图，MiniMax 拿到的上下文包里带着
   「已完成 / 待办 / 上次验证结论」，不会重复做已完成的事。
2. **参与任务推进**：`verify` 与 `demo apply` 会写事件；`step_done` 把任务定义的 4 个步骤
   逐条推进，`待办` 随之减少。
3. **跨会话恢复（实测过）**：本轮开发中把 PowerContext Server 进程杀掉再重启，
   重新 `state show` 仍然读到同一个 Scope `scp_588xvxtd5x499bcd231s2b2793` 与全部历史事件——
   `ensure_scope` 用固定幂等键找回同一个 Scope，不会分裂出第二个状态库。

## 4. 空状态与重置

```bash
python -m sddctx state reset --task TASK-0007   # 写一条 state_reset 标记
python -m sddctx state show  --task TASK-0007   # 事件数 0，待办 4 步
```

`state_reset` 之后只认新事件（`state.py::_after_reset`），因此「从空状态开始记录」在
PowerContext 里也是可复现的，不需要删除 Server 数据。

## 5. 降级（没有 Server 时）

`SDDCTX_STATE=file` 或探测不到 `http://127.0.0.1:8848` 时，状态写到
`runs/state/events.jsonl`，`state.backend` 变成 `file` 并给出 `fallback_reason`：

```
状态后端：file / …/runs/state/events.jsonl
降级原因：PowerContext 不可达：http://127.0.0.1:8848
```

这样评审在没装 PowerContext 的机器上也能验证「状态保存 → 重启读回 → 参与后续上下文」这条链路，
同时又不会误以为用的是 PowerContext。
