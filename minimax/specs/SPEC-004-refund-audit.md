# SPEC-004 退款审计与幂等

> 版本：v1.0 ｜ 状态：生效 ｜ 负责人：风控域 ｜ 最后更新：2026-10-02

## 4.1 审计字段

每一笔退款试算与退款单落库都必须记录：`order_id`、`skus`、`goods_refund_cents`、
`shipping_refund_cents`、`total_refund_cents`、`warnings`、`refunded_cents_before`、`operator`。
缺字段的退款单视为脏数据，需要补偿任务回填。

## 4.2 告警落库

`RefundQuote.warnings` 中的每一条文本都要原样写入审计表，便于风控按月统计
「被上限截断」「运费分摊缺失」等异常占比。告警不得吞掉，也不得改写成自由文本。

## 4.3 幂等键

退款申请必须携带幂等键（`request_id`），同一幂等键重复提交返回首次结果，不得重复扣减
`refunded_cents`。本版本由上层 application service 实现，`RefundService` 不感知幂等键。
