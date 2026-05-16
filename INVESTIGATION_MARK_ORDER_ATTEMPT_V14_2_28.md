# v14.2.28 mark_order_attempt 调查报告

## 结论

本次只修 Telegram 按钮 `float object has no attribute get`。没有修改 `trader.py` 的 `mark_order_attempt()` 触发条件。

## mark_order_attempt 调用清单

1. `trader.py:574`，函数执行主下单流程中，`if config.DRY_RUN:` 分支。
   - 调用：`mark_order_attempt(..., status="dry_run", reason="dry_run_not_submitted")`
   - 此时 post_order 已调用？否。
   - 应不应该锁？生产实盘一般不影响；DRY_RUN 下可接受。

2. `trader.py:624`，执行复查通过、写入 `trades` attempted 后，真实 `post_order` 调用之前。
   - 调用：`mark_order_attempt(..., status="submitting", reason="passed_filters")`
   - 此时 post_order 已调用？否，但即将调用。
   - 应不应该锁？可以。它属于“即将真实下单”的防重复保护。

3. `trader.py:667`，`post_order` 超时异常分支。
   - 调用：`mark_order_attempt(..., status="submit_timeout_unknown", reason="order_submit_timeout")`
   - 此时 post_order 已调用？是，调用结果未知。
   - 应不应该锁？可以。避免同一窗口重复提交。

4. `trader.py:681`，`post_order` 抛异常分支。
   - 调用：`mark_order_attempt(..., status="failed", reason="order_placement_failed")`
   - 此时 post_order 已调用？是。
   - 应不应该锁？可以短锁；是否完全锁整窗需后续按失败类型判断。

5. `trader.py:694`，`post_order` 返回 success=false 分支。
   - 调用：`mark_order_attempt(..., status="failed", reason="order_placement_failed")`
   - 此时 post_order 已调用？是。
   - 应不应该锁？可以短锁；是否完全锁整窗需后续按失败类型判断。

6. `trader.py:712`，`post_order` 返回成功响应后。
   - 调用：`mark_order_attempt(..., order_id=order_id, status=status or "submitted", reason="post_order_response")`
   - 此时 post_order 已调用？是。
   - 应不应该锁？应该。

7. `trader.py:740`，已提交但未撮合 / live / delayed / unmatched 分支。
   - 调用：`mark_order_attempt(..., order_id=order_id, status=status or "not_matched", reason="order_not_matched")`
   - 此时 post_order 已调用？是。
   - 应不应该锁？应该，避免重复刷单。

8. `trader.py:766`，matched 但没有确认 fill 分支。
   - 调用：`mark_order_attempt(..., order_id=order_id, status=status or "unconfirmed", reason="fill_unconfirmed")`
   - 此时 post_order 已调用？是。
   - 应不应该锁？应该。

## 特别确认

`execution_guard.recheck_before_order` 失败分支在 `trader.py:542-545`：

```python
if not exec_check.allowed:
    self.flight_recorder.record(...)
    log.info("Skip: execution recheck failed ...")
    return self._skip(...)
```

此处没有调用 `mark_order_attempt()`。所以 `execution_recheck` 拒绝本身不应该触发 attempt lock。

## 需要用户决定

是否把 `status="submitting"` 的锁改成“只有真正进入 `post_order` 调用时才写入”？当前代码已经是在 `post_order` 前非常接近的位置写入。若要更严格，可以把 `mark_order_attempt(status="submitting")` 下移到 `asyncio.to_thread(self.polymarket.place_buy_order, ...)` 之前一行，并保证 execution_recheck 失败不写锁。

## 关于 execution_weighted_avg_price_too_high

这不是 bug，是保护。示例：`best_ask=0.19`，但 $1 订单要吃多档，实际加权均价达到 `0.57`，超过上限 `0.52`，说明最便宜档流动性太薄。强行买入会把原本便宜的 longshot 买成高价，风险收益比会变差。
