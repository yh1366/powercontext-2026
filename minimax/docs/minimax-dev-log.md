# MiniMax 开发记录

本记录由 `scripts/render_dev_log.py` 从 `runs/` 下的落盘文件自动生成，
内容与提交的 `patches/` 补丁、`docs/logs/` 测试日志一一对应，未做人工改写。

## 1. 接入方式

| 项目 | 值 |
| --- | --- |
| 接口 | `POST {MINIMAX_BASE_URL}/v1/chat/completions`（OpenAI 兼容；服务端不支持时回退 `chatcompletion_v2`） |
| Base URL | `https://api.minimaxi.com` |
| 模型 | `MiniMax-M2.7`（`python -m sddctx models` 可列出该 Key 下全部可用模型） |
| 客户端 | `sddctx/minimax_client.py`，标准库 urllib，无 SDK 依赖 |
| 参数 | temperature=0.1，max_tokens=8000，stream=false |
| 产物 | `runs/<run_id>/minimax.request*.json`、`minimax.response*.json`、`codegen.json`，并归档到 `docs/minimax-run/` |

## 2. 生成轮次一览

每次调用都单独落盘（一次成功就是 1 行，需要重试时每轮各占 1 行），便于核对「模型看到了什么、错在哪、怎么修」。

| 轮 | run_id | request_id | 耗时 | prompt / completion | 结果 |
| --- | --- | --- | --- | --- | --- |
| 1 | `run-20261029T210730-TASK-0007` | `073278174b551dd1` | 57505 ms | 3862 / 3084 | 补丁已应用 |

> 说明：最初上下文只给了「按符号切出来的代码片段」，模型按片段猜行号，输出的 hunk
> 在 `@@ -8` 附近找不到上下文行、补丁打不上（`PatchError`）。把「目标文件完整内容（带行号）」
> 加进上下文后，同一任务一次应用成功——这也是 `context.files` 字段的由来。

## 3. 上下文（喂给模型的第一轮输入）

- 任务：`TASK-0007` 部分退款按商品金额占比分摊运费，并限制累计退款不超过实付金额
- 上下文编号：`run-20261029T210730-TASK-0007`（规格版本 `v1`）
- 资料库：`seekdb` / 库 `sddctx`，检索结果在 `query.traces` 里带命中行数

| 类别 | 来源 ID | 位置 |
| --- | --- | --- |
| spec | `SPEC-003#3.4` | `specs/SPEC-003-refund.md` L30–L38 |
| spec | `SPEC-003#3.2` | `specs/SPEC-003-refund.md` L14–L23 |
| spec | `SPEC-003#3.3` | `specs/SPEC-003-refund.md` L24–L29 |
| spec | `SPEC-001#1.2` | `specs/SPEC-001-order-pricing.md` L10–L21 |
| code | `demo_project/ordercore/refund.py::RefundService.quote_partial_refund` | `demo_project/ordercore/refund.py` L49–L79 |
| code | `demo_project/ordercore/refund.py::<module>` | `demo_project/ordercore/refund.py` L1–L15 |
| code | `demo_project/ordercore/money.py::share` | `demo_project/ordercore/money.py` L10–L23 |
| code | `demo_project/ordercore/pricing.py::member_discount` | `demo_project/ordercore/pricing.py` L30–L36 |
| code | `demo_project/ordercore/__init__.py::<module>` | `demo_project/ordercore/__init__.py` L1–L32 |
| test | `demo_project/tests/test_refund_shipping_allocation.py::test_full_refund_returns_all_shipping` | `demo_project/tests/test_refund_shipping_allocation.py` L30–L34 |
| test | `demo_project/tests/test_refund_shipping_allocation.py::<module>` | `demo_project/tests/test_refund_shipping_allocation.py` L1–L16 |
| test | `demo_project/tests/test_refund_shipping_allocation.py::test_remote_region_shipping_is_allocated_too` | `demo_project/tests/test_refund_shipping_allocation.py` L37–L42 |
| test | `demo_project/tests/test_refund_shipping_allocation.py::test_partial_refund_allocates_shipping_by_goods_share` | `demo_project/tests/test_refund_shipping_allocation.py` L22–L27 |
| test | `demo_project/tests/test_refund_shipping_allocation.py::test_cumulative_refund_never_goes_negative` | `demo_project/tests/test_refund_shipping_allocation.py` L66–L71 |
| test | `demo_project/tests/test_refund_shipping_allocation.py::test_allocation_rounds_half_up` | `demo_project/tests/test_refund_shipping_allocation.py` L45–L55 |
| test | `demo_project/tests/test_refund_shipping_allocation.py::test_cumulative_refund_is_capped_by_paid_amount` | `demo_project/tests/test_refund_shipping_allocation.py` L58–L63 |
| 完整文件 | `demo_project/ordercore/refund.py` | L1–L79（`b07617c32cf8`） |
| 完整文件 | `demo_project/ordercore/money.py` | L1–L31（`34baf1bbf0e4`） |

检索 trace（spec 类）：`[{"step": "df", "rows": 69}, {"step": "postings", "rows": 159}, {"step": "chunks", "rows": 12}, {"step": "documents", "rows": 4}]`

## 4. 提示词（最终轮 = 第 1 轮）

- messages 条数：2（system + user）
- user 消息字符数：9557

system 提示词：

```
你是一名严格遵守规格的 Python 工程师。你会收到一份 SDD 上下文：任务描述、必须遵守的规格条目、需要修改的代码、必须继续通过的既有测试，以及用于验证的验收测试。
要求：
1. 只修改生产代码，禁止修改任何测试文件、禁止删除或弱化断言；
2. 严格遵守给出的规格条目（含取整口径、边界条件）；
3. 不引入新的第三方依赖，不改变已有函数签名，保持既有行为；
4. 只输出一个 ```diff 代码块（unified diff，a/ 与 b/ 前缀为 demo_project/ 下的相对路径），不要输出解释、不要输出完整文件。
5. diff 必须能被 `git apply` 直接应用：hunk 头 `@@ -旧起始行,行数 +新起始行,行数 @@` 的行号以「目标文件完整内容」左侧行号为准；hunk 内的上下文行以单个空格开头、逐字复制，中间的未改动行不得省略。
```

user 消息（前 40 行，完整内容见 `runs/run-20261029T210730-TASK-0007/minimax.request.json`）：

```
下面是本次开发任务的上下文包（由 seekdb 检索结果组装）：

任务：TASK-0007 部分退款按商品金额占比分摊运费，并限制累计退款不超过实付金额

退款服务当前只退还商品金额，运费一律不退：部分退款时用户少收运费，整单退款时退款总额也不等于实付金额；多次退款之间还没有累计校验，历史数据里出现过累计退款超过实付的脏账。本次任务按 SPEC-003#3.2 实现运费按商品金额占比分摊、按 SPEC-003#3.4 实现可退余额上限与告警，并且不得改变既有计价、运费与退款行为。

必须遵守的规格条目：
- SPEC-003#3.4（specs/SPEC-003-refund.md 第 30-38 行）
## 3.4 累计退款上限

同一订单允许多次部分退款，但**累计退款金额（商品退款 + 运费退款）不得超过实付金额**：

1. 可退余额 = `订单实付金额 - 已退金额`（`refunded_cents`）。
2. 本次试算总额超过可退余额时，截断到可退余额，并在 `RefundQuote.warnings` 中返回告警文本。
3. 可退余额小于 0（历史数据异常、已超额退款）时，本次可退金额为 0，不得返回负数，同样返回告警。
4. 未触及上限的正常退款不得产生告警，以免告警泛滥导致人工复核失效。

- SPEC-003#3.2（specs/SPEC-003-refund.md 第 14-23 行）
## 3.2 运费分摊

退款必须按商品金额占比分摊订单运费，不允许把运费整笔吞掉或整笔退还：

1. 分摊比例 = 本次退款商品金额 / 订单商品金额合计（折扣前口径，与 SPEC-003#3.3 一致）。
2. 运费退款 = `money.share(订单运费, 退款商品金额, 商品金额合计)`，即先做精确十进制除法，再 HALF_UP 取整到分。
3. 当本次退款覆盖订单全部商品时，直接退还全部运费，避免占比相除后出现 1 分尾差。
4. 订单运费为 0（包邮）时，运费退款为 0，且不得触发除零错误。
5. 运费的来源是订单快照的 `shipping_cents`，不重新按 SPEC-002 计算运费。

- SPEC-003#3.3（specs/SPEC-003-refund.md 第 24-29 行）
## 3.3 折扣与优惠券回退

会员折扣与优惠券按与 #3.2 相同的商品金额占比回退：`money.share(订单折扣, 退款商品金额, 商品金额合计)`、
`money.share(订单优惠券, 退款商品金额, 商品金额合计)`。商品退款金额 = 退款商品金额 − 折扣回退 − 优惠券回退。
回退金额不得为负，也不得超过订单实际使用的折扣/优惠券总额。

- SPEC-001#1.2（specs/SPEC-001-order-pricing.md 第 10-21 行）
## 1.2 会员折扣

会员折扣按商品金额合计计算，使用 `money.apply_rate` 以 HALF_UP 取整到分：

…
```

## 5. 模型响应（最终轮）

| 指标 | 值 |
| --- | --- |
| model | `MiniMax-M2.7` |
| endpoint | `https://api.minimaxi.com/v1/chat/completions` |
| request_id | `073278174b551dd17b3cfe3d52e6feca` |
| 端到端耗时 | 57505 ms |
| usage | `{"total_tokens": 6946, "total_characters": 0, "prompt_tokens": 3862, "completion_tokens": 3084, "completion_tokens_details": {"reasoning_tokens": 2636}, "prompt_tokens_details": {"cached_tokens": 128}}` |
| 输出字符数 | 1653 |

模型输出原文：

```diff
```diff
--- a/demo_project/ordercore/refund.py
+++ b/demo_project/ordercore/refund.py
@@ -58,15 +58,22 @@ class RefundService:
         goods_total = order.goods_cents
         refund_goods = sum(lines[sku].amount_cents for sku in wanted)
 
         # SPEC-003#3.3：会员折扣与优惠券按商品金额占比回退。
         discount_back = money.share(order.discount_cents, refund_goods, goods_total)
         coupon_back = money.share(order.coupon_cents, refund_goods, goods_total)
         goods_refund = refund_goods - discount_back - coupon_back
 
-        # TODO(TASK-0007): SPEC-003#3.2 运费分摊尚未实现，暂时不退还运费。
-        shipping_refund = 0
+        # SPEC-003#3.2：运费按退款商品金额占比分摊，全额退款时直接退还全部运费。
+        if refund_goods == goods_total:
+            shipping_refund = order.shipping_cents
+        else:
+            shipping_refund = money.share(order.shipping_cents, refund_goods, goods_total)
 
         total_refund = goods_refund + shipping_refund
+        refundable_balance = order.paid_cents - order.refunded_cents
         warnings: tuple[str, ...] = ()
-        if refund_goods == goods_total:
-            warnings = ("shipping allocation missing for full refund (SPEC-003#3.2)",)
+        if total_refund > refundable_balance:
+            total_refund = max(0, refundable_balance)
+            goods_refund = min(goods_refund, total_refund)
+            shipping_refund = min(shipping_refund, total_refund - goods_refund)
+            warnings = ("refund exceeds refundable balance; refund truncated (SPEC-003#3.4)",)
         return RefundQuote(
             skus=wanted,
             goods_refund_cents=goods_refund,
             shipping_refund_cents=shipping_refund,
```
```

## 6. 提取出的补丁

- 补丁文件：`D:\oceanbase\wt-qoder-official\minimax\patches\TASK-0007.patch`
- diff sha256：`cc2cd064568b27f9a5113fd9c9eafd8ddc93681c91c7c6326b537d74253bc02f`
- 应用结果：1 个 hunk，+11 / -4 行，改动文件 ordercore/refund.py

### `patches/TASK-0007.patch`

```diff
# TASK-0007 — 由 MiniMax（MiniMax-M2.7）在 SDD 上下文下生成的代码修改
# 生成时间：2026-10-29T21:08:32+08:00  |  request_id：073278174b551dd17b3cfe3d52e6feca
# 应用方式：cd minimax/demo_project && git apply -p1 ../patches/TASK-0007.patch
# 也可以：python -m sddctx demo apply TASK-0007
--- a/demo_project/ordercore/refund.py
+++ b/demo_project/ordercore/refund.py
@@ -58,15 +58,22 @@ class RefundService:
         goods_total = order.goods_cents
         refund_goods = sum(lines[sku].amount_cents for sku in wanted)
 
         # SPEC-003#3.3：会员折扣与优惠券按商品金额占比回退。
         discount_back = money.share(order.discount_cents, refund_goods, goods_total)
         coupon_back = money.share(order.coupon_cents, refund_goods, goods_total)
         goods_refund = refund_goods - discount_back - coupon_back
 
-        # TODO(TASK-0007): SPEC-003#3.2 运费分摊尚未实现，暂时不退还运费。
-        shipping_refund = 0
+        # SPEC-003#3.2：运费按退款商品金额占比分摊，全额退款时直接退还全部运费。
+        if refund_goods == goods_total:
+            shipping_refund = order.shipping_cents
+        else:
+            shipping_refund = money.share(order.shipping_cents, refund_goods, goods_total)
 
         total_refund = goods_refund + shipping_refund
+        refundable_balance = order.paid_cents - order.refunded_cents
         warnings: tuple[str, ...] = ()
-        if refund_goods == goods_total:
-            warnings = ("shipping allocation missing for full refund (SPEC-003#3.2)",)
+        if total_refund > refundable_balance:
+            total_refund = max(0, refundable_balance)
+            goods_refund = min(goods_refund, total_refund)
+            shipping_refund = min(shipping_refund, total_refund - goods_refund)
+            warnings = ("refund exceeds refundable balance; refund truncated (SPEC-003#3.4)",)
         return RefundQuote(
             skus=wanted,
             goods_refund_cents=goods_refund,
             shipping_refund_cents=shipping_refund,
```

## 7. 人工复核与后续验证

- 每轮响应都先经过 `apply_unified_diff` 校验能否真正落到代码上，再进测试；
- 生成后依次执行 `demo apply` → 目标测试 → 回归测试 → `verify --phase after`，结果写回 PowerContext；
- 提交的 `patches/TASK-0007.patch`、`snapshots/` 与最后一轮响应保持一致。

## 附：如何复跑

```bash
cd minimax
export MINIMAX_API_KEY=...           # 或用 .env.local（不入库）
python -m sddctx demo reset           # 回到修改前
python -m sddctx codegen --task TASK-0007 --apply
python scripts/render_dev_log.py
```
