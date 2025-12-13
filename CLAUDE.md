# 项目概述

Clove 是一个 Claude.ai 反向代理，通过 Claude.ai 账户提供标准 Claude API 访问。支持两种模式：
- **OAuth 模式**：通过 OAuth 认证完整访问 Claude API（类似 Claude Code 使用的方式）
- **网页代理模式**：模拟 Claude.ai 网页界面的回退方案

# 开发启动

## 前后端分离开发（推荐）

```bash
# 终端 1：启动后端（端口 5201）
python -m app.main

# 终端 2：启动前端fenz（端口 5173，热重载）
cd front
pnpm install  # 首次需要
pnpm dev

# 访问 http://localhost:5173（前端会自动代理 API 到后端）
```

## 仅后端开发

```bash
python -m app.main
# 访问 http://localhost:5201（无前端界面）
```

# 其他命令

```bash
# 代码检查
ruff check app/
ruff format app/

# 构建 wheel 包（需要先构建前端）
python scripts/build_wheel.py

# 安装为可编辑包（需要先构建前端到 app/static/）
pip install -e ".[rnet,dev]"
```

# 依赖管理

项目使用 **uv** 管理依赖，采用可选依赖设计：

| 依赖组 | 用途 |
|--------|------|
| 核心 | FastAPI, Pydantic 等基础依赖 |
| `rnet` | rnet HTTP 客户端（推荐） |
| `curl` | curl-cffi HTTP 客户端（备选） |
| `dev` | 开发工具（ruff, build） |

## 常用命令

```bash
# 锁文件管理
uv lock              # 根据 pyproject.toml 更新 uv.lock
uv lock --check      # 检查锁文件是否同步（CI/提交前）

# 安装依赖
uv sync              # 只装核心依赖
uv sync --extra rnet # 核心 + rnet
uv sync --all-extras # 安装全部依赖
```

## 工作流

```
pyproject.toml ──uv lock──> uv.lock ──uv sync──> .venv/
   (配置)                    (锁定)              (安装)
```

- `uv.lock` 需提交到 git，保证团队环境一致
- 本地 `--extra` 选项不影响锁文件

# 架构

## 请求处理流程

```
客户端请求 → /v1/messages
       ↓
   ClaudeAIPipeline (app/processors/claude_ai/pipeline.py)
       ↓
   [处理器链 - 按顺序执行]
       ↓
   响应流 → 客户端
```

## 处理器管道

请求处理采用管道模式，处理器位于 `app/processors/claude_ai/`：

1. `TestMessageProcessor` - 处理 SillyTavern 测试消息
2. `ToolResultProcessor` - 处理工具调用结果
3. `ClaudeAPIProcessor` - OAuth API 请求（优先）
4. `ClaudeWebProcessor` - 网页接口回退
5. `EventParsingProcessor` - 解析 SSE 事件
6. `ModelInjectorProcessor` - 注入模型信息
7. `StopSequencesProcessor` - 处理停止序列
8. `ToolCallEventProcessor` - 处理工具调用
9. `MessageCollectorProcessor` - 收集消息内容
10. `TokenCounterProcessor` - 估算 Token 用量
11. `StreamingResponseProcessor` - 格式化流式输出
12. `NonStreamingResponseProcessor` - 格式化非流式输出

## 核心服务（单例）

| 服务 | 文件 | 用途 |
|------|------|------|
| `account_manager` | `app/services/account.py` | 账户生命周期、负载均衡、OAuth Token 刷新 |
| `session_manager` | `app/services/session.py` | Claude.ai 会话管理 |
| `tool_call_manager` | `app/services/tool_call.py` | 待处理工具调用追踪 |
| `cache_service` | `app/services/cache.py` | 响应缓存 |
| `oauth_authenticator` | `app/services/oauth.py` | OAuth 流程处理 |
| `proxy_service` | `app/services/proxy.py` | 动态代理池管理（轮换、健康检查） |

## API 路由

- `/v1/messages` - Claude API 兼容端点
- `/api/admin/accounts` - 账户管理
- `/api/admin/settings` - 配置管理
- `/api/admin/proxies` - 代理列表管理
- `/api/admin/statistics` - 使用统计
- `/health` - 健康检查

## 配置

配置优先级（从高到低）：
1. JSON 配置文件（`~/.clove/data/config.json`）
2. 环境变量
3. `.env` 文件
4. 默认值

核心配置在 `app/core/config.py`，完整选项见 `.env.example`。

## 数据存储

默认位置：`~/.clove/data/`
- `accounts.json` - 账户凭证和 OAuth Token
- `config.json` - 运行时配置
- `proxies.txt` - 动态代理列表（每行一个代理）

# 前端子模块

前端位于 `front/` 子模块 → 独立仓库 `clove-front`（React 19 + Vite 7 + Tailwind CSS 4）

详细文档见 `front/CLAUDE.md`

## 构建部署

```bash
cd front
pnpm install
pnpm build
cp -r dist/* ../app/static/
```

# 关键模式

- **全异步**：所有 I/O 操作使用 `async/await`
- **Pydantic 模型**：请求/响应验证在 `app/models/`
- **Loguru 日志**：使用 `from loguru import logger`
- **Context 模式**：`ClaudeAIContext` 在管道中传递请求状态
