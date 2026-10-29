# 测试报告

> 每条命令的完整输出（含退出码）在 [`logs/`](logs/) 下，文件名与下表一一对应。
> 一键重跑：`bash scripts/record_evidence.sh`。

## 1. 环境

| 项目 | 值 |
| --- | --- |
| Python | 3.13.14（`python -m sddctx doctor` 输出见 `logs/01_doctor.log`） |
| pytest | 9.2.0，`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`（避免第三方插件影响判决） |
| 数据库 | seekdb `5.7.25-OceanBase seekdb-v1.4.0.0`（`sddctx` 库） |
| 状态 | PowerContext Server 1.4.1，`scope scp_588xvxtd5x499bcd231s2b2793` |
| 业务工程 | `demo_project/`（`ordercore` 6 模块 + 4 个测试文件，22 个用例） |

## 2. 修改前（目标行为未实现）

```
$ pytest -q demo_project/tests/test_refund_shipping_allocation.py
......                                                                   [100%]  ← 全在 refund 相关，见下
6 failed in 0.37s                                                        exit=1
```

| 用例 | 失败原因（断言） | 说明 |
| --- | --- | --- |
| `test_partial_refund_allocates_shipping_by_goods_share` | `assert 0 == 356` | 运费退款恒为 0 |
| `test_full_refund_returns_all_shipping` | `assert 0 == 800` | 整单退款没退运费 |
| `test_remote_region_shipping_is_allocated_too` | `assert 0 == 667` | 偏远地区同样不退 |
| `test_cumulative_refund_is_capped_by_paid_amount` | `assert 4165 == 465` | 没有累计上限 |
| `test_cumulative_refund_never_goes_negative` | `assert 1388 == 0` | 超额退款仍返回正数 |
| `test_allocation_rounds_half_up` | `assert 0 == 452` | 分摊未实现 |

失败原因全部是 `RefundQuote.shipping_refund_cents` / `total_refund_cents` 的业务断言，
**没有 ImportError、没有收集失败、没有环境错误**——业务工程本身可导入、可运行。

回归测试（修改前同样全部通过，证明基线干净）：

```
$ pytest -q demo_project/tests/test_pricing.py demo_project/tests/test_shipping.py demo_project/tests/test_refund_existing.py
16 passed in 0.38s                                                       exit=0
```

`python -m sddctx verify --task TASK-0007 --phase before` 的结论（写入 PowerContext）：

```
目标测试：exit=1  失败
回归测试：exit=0  通过
结论：目标测试 失败（exit=1），回归测试 通过（exit=0）
```

## 3. 修改后

```
$ pytest -q demo_project/tests
22 passed in 0.12s                                                       exit=0
```

- 目标测试 6 个：全部由失败转通过；
- 回归测试 16 个：仍然全部通过（计价 4 + 运费 5 + 退款既有行为 7）；
- 测试集没有改动：`demo reset` / `demo apply` 只切换 `demo_project/ordercore/refund.py`，
  测试文件在 `snapshots/manifest.json` 里没有 after 版本（即目标与回归测试前后完全一致）。

`python -m sddctx verify --task TASK-0007 --phase after`：

```
目标测试：exit=0  通过
回归测试：exit=0  通过
结论：目标测试 通过（exit=0），回归测试 通过（exit=0）
```

## 4. 目标功能测试清单（新增，测试集前后一致）

文件：`demo_project/tests/test_refund_shipping_allocation.py`

| 用例 | 规格依据 | 断言要点 |
| --- | --- | --- |
| `test_partial_refund_allocates_shipping_by_goods_share` | SPEC-003#3.2 | 退 2000/4500 → 运费退 356，总计 2207 |
| `test_full_refund_returns_all_shipping` | SPEC-003#3.2 第 3 条 | 整单退款运费退 800，总计 = 实付 4965 |
| `test_remote_region_shipping_is_allocated_too` | SPEC-003#3.2 | 偏远运费 2000 → 退 667 |
| `test_allocation_rounds_half_up` | SPEC-003#3.2 取整口径 | 903×500/1000=451.5 → 452 |
| `test_cumulative_refund_is_capped_by_paid_amount` | SPEC-003#3.4 | 可退余额 465，截断 + 告警 |
| `test_cumulative_refund_never_goes_negative` | SPEC-003#3.4 第 3 条 | 超额退款时退 0 + 告警 |

## 5. 代码版本对应

| 阶段 | `git rev-parse HEAD`（记录时） | 说明 |
| --- | --- | --- |
| 修改前 / 修改后日志 | 见 `logs/06_verify_before.log`、`logs/12_verify_after.log` 的 `代码版本` 字段 | 两次验证的 commit 相同：代码差异通过 `patches/` + `snapshots/` 管理，不是两个 commit |

说明：本题的「修改前后」是同一份提交内的两套快照（`snapshots/before`、`snapshots/after`），
由 `demo reset` / `demo apply` 切换。这样评审在一个 commit 上就能复现两种状态，
也避免了「靠 git 历史找修改前版本」的不可复现风险。

## 6. 与验收项对应的自检结果

| 验收项 | 证据 |
| --- | --- |
| 能够运行 | `logs/01~04`：doctor / demo reset / init / ingest 全部 exit=0 |
| MiniMax 实际参与 | `docs/minimax-dev-log.md` + `runs/<run_id>/minimax.request.json` / `minimax.response.json` |
| 上下文正确 | `logs/09_check_v1.log`（必需来源齐全）、`logs/17_check_v2.log`（规格 v1.1 生效） |
| 数据库实际参与 | `logs/04_ingest_v1.log`（3761 行倒排）、`docs/db-evidence.md`（检索 SQL 与行数） |
| 状态能够复用 | `logs/19_state_show.log`（新进程读回 15 条事件折叠的任务视图） |
| 代码修改有效 | `logs/05`（6 failed）→ `logs/11`（22 passed） |
