# 验证样例

本样例可被完整重放：**任务描述 → 有效规格 → 初始数据 → 操作步骤 → 预期结果 → 重置方法**。
所有日志见 [`logs/`](logs/)，一键重跑：`bash scripts/record_evidence.sh`。

---

## 1. 任务描述（要改什么）

`TASK-0007`：退款服务目前只退还商品金额（含折扣与优惠券回退），**运费一律不退**；
并且多次退款之间**没有累计校验**，历史数据出现过累计退款超过实付的脏账。

要改的是 `demo_project/ordercore/refund.py` 的 `RefundService.quote_partial_refund`：

1. 按 `SPEC-003#3.2` 把订单运费按商品金额占比分摊到本次退款（整单退款退全部运费，包邮订单退 0）；
2. 按 `SPEC-003#3.4` 用可退余额（实付 − 已退）截断本次退款，超出/异常时返回告警。

## 2. 必须保持的原有行为（哪些不能变）

| 范围 | 断言 | 文件 |
| --- | --- | --- |
| 计价 | 银卡 3% / 金卡 8% 折扣、优惠券不超过折后金额、包邮门槛看折后金额 | `tests/test_pricing.py`（4 个） |
| 运费 | 普通地区 8 元 / 满 99 包邮；偏远 20 元 / 满 199 包邮 | `tests/test_shipping.py`（5 个） |
| 退款既有行为 | 商品金额按占比回退折扣与优惠券、重复 SKU 去重、未知 SKU 与空列表拒绝、包邮订单不除零 | `tests/test_refund_existing.py`（7 个） |

## 3. 有效规格（上下文中必须出现的条款）

| 规格 | 条款 | 文件 / 行 | 关键内容 |
| --- | --- | --- | --- |
| `SPEC-003#3.2` | 运费分摊 | `specs/SPEC-003-refund.md` L14–L23 | `money.share(订单运费, 退款商品金额, 商品金额合计)`，整单退款退全部运费 |
| `SPEC-003#3.4` | 累计退款上限 | `specs/SPEC-003-refund.md` L30–L38 | 可退余额 = 实付 − 已退；超额截断并告警；余额为负时退 0 |
| `SPEC-003#3.3` | 折扣与优惠券回退 | 同上 L24–L29 | 与运费同一占比口径（不改，仅作上下文参照） |

## 4. 上下文中必须包含的来源（评审核对清单）

| 类别 | 来源 ID | 说明 |
| --- | --- | --- |
| 规格 | `SPEC-003#3.2`、`SPEC-003#3.4` | 本次任务的两条约束 |
| 代码 | `demo_project/ordercore/refund.py::RefundService.quote_partial_refund` | 需要修改的函数 |
| 代码 | `demo_project/ordercore/money.py::share` | 规格指定的分摊取整工具 |
| 测试 | `…test_refund_shipping_allocation.py::test_partial_refund_allocates_shipping_by_goods_share` | 部分退款分摊 |
| 测试 | `…::test_full_refund_returns_all_shipping` | 整单退款退全部运费 |
| 测试 | `…::test_cumulative_refund_is_capped_by_paid_amount` | 累计上限截断 + 告警 |

以上清单写在 `tasks/TASK-0007.json` 的 `required_sources`，由 `python -m sddctx check` 逐条核对，
日志见 [`logs/09_check_v1.log`](logs/09_check_v1.log)（exit 0）。

## 5. 初始数据

| 项目 | 值 |
| --- | --- |
| 规格 | `specs/SPEC-001..004`（v1 生效版本：SPEC-003 为 v1.0）；`specs/variants/SPEC-003-refund.v2.md` 为 v1.1 演进版本 |
| 业务工程 | `demo_project/`（`ordercore` 6 个模块 + 4 个测试文件，纯标准库） |
| 修改前代码 | `snapshots/before/demo_project/ordercore/refund.py`（`shipping_refund = 0` + TODO） |
| 修改后代码 | `snapshots/after/demo_project/ordercore/refund.py`；差异 `patches/TASK-0007.patch` |
| 资料库 | seekdb 库 `sddctx`，`pc_documents` 16 行 / `pc_chunks` 76 行 / `pc_postings` 3698 行 |
| 状态 | PowerContext scope `scp_588xvxtd5x499bcd231s2b2793`（`state reset` 后可复现空状态） |
| 订单夹具 | `tests/conftest.py::build_order`：商品 4500 → 银卡折扣 135 → 券 200 → 运费 800 → 实付 4965 |

## 6. 操作步骤与预期结果

```bash
cd minimax
pip install -r requirements.txt
python -m sddctx init && python -m sddctx ingest --reset
```

| # | 命令 | 预期 | 日志 |
| --- | --- | --- | --- |
| 1 | `python -m sddctx doctor` | 打印 `seekdb`、`powercontext`、`key=已配置/未配置` | `01_doctor.log` |
| 2 | `python -m sddctx demo reset` | 恢复 `refund.py` 到修改前 | `02_demo_reset.log` |
| 3 | `pytest -q demo_project/tests/test_refund_shipping_allocation.py` | **6 failed**，exit 1（断言失败，不是环境错误） | `05_pytest_before.log` |
| 4 | `python -m sddctx verify --task TASK-0007 --phase before` | 目标测试失败 / 回归测试通过；退出码 1 | `06_verify_before.log` |
| 5 | `pytest -q …/test_pricing.py …/test_shipping.py …/test_refund_existing.py` | **16 passed**，exit 0 | `07_pytest_regression.log` |
| 6 | `python -m sddctx context --task TASK-0007` | 输出规格 4 条 / 代码 5 条 / 测试 7 条 + 目标文件全文 + 任务状态 | `08_context_v1.log` |
| 7 | `python -m sddctx check --task TASK-0007` | `必需来源齐全：是`，exit 0 | `09_check_v1.log` |
| 8 | `python -m sddctx demo apply` | 应用修改后快照 | `10_demo_apply.log` |
| 9 | `pytest -q demo_project/tests` | **22 passed**，exit 0 | `11_pytest_after.log` |
| 10 | `python -m sddctx verify --task TASK-0007 --phase after` | 目标 + 回归全部通过；退出码 0 | `12_verify_after.log` |
| 11 | `python -m sddctx ingest --variant v2` | 规格 v1.1 覆盖 v1.0：`规格版本：v2` | `15_ingest_v2.log` |
| 12 | `python -m sddctx check --task TASK-0007 --variant v2` | 上下文引用 v1.1，命中新增条款「尾差归入…」 | `16~17_*.log` |
| 13 | `python -m sddctx state show --task TASK-0007` | 读回 4 个已完成步骤 + 2 条验证记录（新进程） | `19_state_show.log` |
| 14 | `python -m sddctx demo sample` | 一条命令跑完第 2–13 步，结论行三项均为 True | `20_demo_sample.log` |

一步到位：`python -m sddctx demo sample`（会把上面第 2–13 步全部跑一遍并打印判定结论，日志 `20_demo_sample.log`）。

### 第 3 步失败长什么样（不是环境错误）

```
FAILED demo_project/tests/test_refund_shipping_allocation.py::test_partial_refund_allocates_shipping_by_goods_share
E    assert 0 == 356
E     +  where 0 = RefundQuote(skus=('SKU-B',), goods_refund_cents=1851,
                                shipping_refund_cents=0, total_refund_cents=1851, warnings=()).shipping_refund_cents
```

业务工程的导入、计价、运费、既有退款用例全部正常（回归 16 个用例 exit 0），
失败原因明确是「运费分摊未实现」这一功能缺失。

### 第 5 步上下文输出长什么样

```
## 适用规格条目（4）
### `SPEC-003#3.4`   specs/SPEC-003-refund.md 第 30–38 行   相关度 72.05
### `SPEC-003#3.2`   specs/SPEC-003-refund.md 第 14–23 行   相关度 55.41
## 需要修改的代码（5）
### `demo_project/ordercore/refund.py::RefundService.quote_partial_refund`  第 49–79 行
## 验证用测试（7）
### `…::test_partial_refund_allocates_shipping_by_goods_share` / `…::test_full_refund_returns_all_shipping` / …
## 目标文件完整内容
### `demo_project/ordercore/refund.py`（79 行）  ← 带行号的整份文件，模型据此算 hunk 行号
## 任务状态（PowerContext）
- 已完成：…（首次运行为空）  - 待办：4 步
```

### 第 11 步「规格演进」的证据

`SPEC-003#3.2` 在 v1.1 里新增第 6 条（多行退款尾差归属）。重新导入后：

```
- 规格版本：`v1.1`  文件：`specs/variants/SPEC-003-refund.v2.md`
- 标记「尾差归入分摊后金额最大的商品行」：命中（SPEC-003#3.2）
```

v1.0 的同一条款不会同时出现：`pc_documents` 里 `SPEC-003` 只有一行（路径已切换为 variants），
旧块被替换而不是并存——这就是「输出不是固定答案」的机制。

## 7. 重置方法

```bash
# 业务工程：回到修改前（也可以 python -m sddctx demo apply 回到修改后）
python -m sddctx demo reset

# 资料库 + 索引：清空重建（保留表结构）
python -m sddctx ingest --reset

# 规格版本：回到 v1
python -m sddctx ingest

# 任务状态：清空（PowerContext 里写一条 state_reset 标记，之后的读回只认新事件）
python -m sddctx state reset --task TASK-0007

# 状态后端降级验证（没有 PowerContext Server 时）
SDDCTX_STATE=file python -m sddctx state show --task TASK-0007

# 完全不用数据库（SQLite 降级）
SDDCTX_DB=sqlite SDDCTX_HOME=/tmp/sddctx python -m sddctx demo sample
```

`demo reset` 依据 `snapshots/manifest.json` 里的 sha256 清单逐文件恢复，
不依赖 git 是否安装，也不依赖工作区是否干净。
