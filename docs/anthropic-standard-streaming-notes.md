# Anthropic 标准流式输出收敛记录

## 背景

Clove 代理 Claude Web 请求链路时，上游会返回 Claude Web 私有流式事件和数据结构。
直接透传给 API 客户端（如 CherryStudio）会触发类型校验错误。

**默认策略**：以 Anthropic Messages API 标准输出为准，优先保证通用客户端可用性。

1. 仅输出已建模的标准流式事件
2. 对可映射的私有事件做标准化转换（如 `citation_start_delta` → `citations_delta`）
3. 对不可映射的私有事件直接跳过，不向客户端透传
4. 对 Claude Web 私有 `tool_result` 块（如 `knowledge` 列表）仅内部消费，不对外输出

---

## 事件映射矩阵

| 上游事件 | 处理方式 | 输出形态 | 说明 |
|----------|----------|----------|------|
| `message_start` | 透传 | `message_start` | 标准事件 |
| `content_block_start` | 透传 | `content_block_start` | 标准事件 |
| `content_block_delta` (`text_delta`) | 透传 | `text_delta` | 标准事件 |
| `content_block_delta` (`thinking_delta`) | 透传 | `thinking_delta` | 标准事件 |
| `content_block_delta` (`input_json_delta`) | 透传 | `input_json_delta` | 标准事件 |
| `content_block_delta` (`signature_delta`) | 透传 | `signature_delta` | 标准事件 |
| `content_block_delta` (`citation_start_delta`) | **映射** | `citations_delta` | 私有→标准：转为 `web_search_result_location` |
| `content_block_delta` (`citation_end_delta`) | **丢弃** | — | 不含额外来源信息 |
| `content_block_delta` (`thinking_summary_delta`) | **丢弃** | — | 私有摘要事件，标准 API 无对应 |
| `content_block_stop` | 透传 | `content_block_stop` | 标准事件 |
| `message_delta` | 透传 | `message_delta` | 标准事件 |
| `message_stop` | 透传 | `message_stop` | 标准事件 |
| `message_limit` | **丢弃** | — | Claude Web 私有限额通知 |
| 私有 `tool_result`（`knowledge` 列表） | **内部消费** | — | 仅聚合到 collected_message，不对外输出 |

实现位置：`app\services\event_processing\event_parser.py` (`EventParser`) + `app\processors\claude_ai\streaming_response_processor.py` (`EventSerializer`)，均默认 `skip_unknown_events=True`。

---

## 标准化引用输出

### 决策

- 不做"正文追加 Sources 文本"的兜底兼容
- 仅做 Anthropic 标准结构：`text.citations` + `content_block_delta.delta.type=citations_delta`

### 实现

| 目标 | 实现 |
|------|------|
| 非流响应携带引用 | `TextContent` 增加 `citations` 字段（`List[TextCitation]`） |
| 流式支持标准引用增量 | `Delta` Union 增加 `CitationsDelta`（`type="citations_delta"`） |
| Claude Web 私有引用事件转标准 | `EventParser._normalize_private_event()` 将 `citation_start_delta` 转为 `citations_delta`，映射为 `web_search_result_location` |
| 收集器聚合最终消息引用 | `MessageCollectorProcessor._apply_delta()` 将 `CitationsDelta` 追加到对应 `TextContent.citations` |

### 说明

- `citation_end_delta` 本身不包含额外来源信息，当前不需要参与标准化输出
- `thinking_summary_delta`、`message_limit` 仍按未知事件处理策略执行（默认跳过）

---

## 非流式兼容补充

### 现象

CherryStudio 非流式调用报错 `Invalid JSON response`，校验路径指向 `content[*].signature` 期望 `string`。

### 根因

非流响应由流式聚合得到，`thinking` 块在聚合过程中未稳定提供 `signature` 字段。

### 修复

1. 在 `MessageCollectorProcessor._apply_delta()` 中处理 `SignatureDelta`，写入 `thinking.signature`
2. 在 `thinking` 块创建（`content_block_start`）和 `message_stop` 时增加兜底：若缺失则补 `signature: ""`

### 结果

非流响应中的每个 `thinking` 块都包含字符串类型 `signature`，满足客户端的 Anthropic 类型校验。

---

## 调试记录归档（2026-02-25）

> 以下为开发过程中的排查记录，供后续参考。

### 现象（CherryStudio + Clove）

在开启 `thinking` + `web_search` 时，客户端先收到 thinking 内容和文本 `"Let me search for current news for you."`，随后流提前结束或缺块。

### 根因与修复

| 问题 | 根因 | 修复 |
|------|------|------|
| 提前 `message_stop` | `ToolCallEventProcessor` 把服务端 `web_search` 的 `tool_use` 当成客户端工具调用，注入 `stop_reason=tool_use` 并中断流 | `tool_call_event_processor.py` 区分 server web_search tool，不再触发暂停 |
| 思维摘要/引用/message_limit 缺失 | 未建模的私有事件被 parser/serializer 静默丢弃 | 默认 `skip_unknown_events=True` 跳过未知事件，对可映射的私有事件做标准化转换 |
| `tool_result` 块缺失 | `ToolCallEventProcessor` 对所有 `tool_result` 块统一跳过 | 私有 `tool_result`（`knowledge` 列表）仅内部消费，不对外透传 |
| 放开 `tool_result` 后流中断 | `MessageCollectorProcessor` 重建 `ToolResultContent` 时强校验 `content`，web_search 返回 `knowledge` 项导致校验异常 | 改为解析后直接写回 `block.content`（保留原始结构）并异常兜底 |

### 验证结论

Clove 代理输出的块序列 `thinking → text → tool_use → tool_result → thinking → text` 与直连 Claude.ai 的结构一致（内容长度和搜索轮次因模型实时决策而不同）。

---

## 后续待办

- [ ] 评估 `thinking_summary_delta` 是否可映射到标准结构（当前丢弃）
- [ ] 建立自动化回归：同请求对比 Clove 输出与 Anthropic 官方 Messages API 输出
