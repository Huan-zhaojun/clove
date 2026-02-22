# 代理设置功能

## 概述

Clove 支持三种代理模式，用于避免单个 IP 请求频率过大、403 封禁、以及单个账户频繁切换 IP 等问题。

## 代理模式

| 模式 | 说明 |
|------|------|
| `disabled` | 不使用代理 |
| `fixed` | 固定代理（单一 URL） |
| `dynamic` | 动态代理池（轮换策略） |

## 配置

### config.json

```json
{
  "proxy_url": "http://old.proxy:8080",  // 旧配置，向后兼容

  "proxy": {
    "mode": "dynamic",
    "fixed_url": "http://fixed.proxy:8080",
    "rotation_strategy": "sequential",
    "rotation_interval": 300,
    "cooldown_duration": 1800,
    "fallback_strategy": "sequential"
  }
}
```

### 配置优先级

- `proxy.mode` 存在 → 使用新配置
- 否则 → 使用旧配置 `proxy_url`（视为 fixed 模式）

### 配置迁移

启动时自动执行 `migrate_proxy_config()`：

- 检测旧的 `proxy_url` 配置
- 转换为新的 `ProxySettings` 格式（mode=fixed）
- 更新 `config.json` 文件

### proxies.txt

位置：`~/.clove/data/proxies.txt`

```
# 代理列表文件
# 一行一个代理，支持多种格式
# 空行和 # 开头的注释行会被忽略

http://proxy1.example.com:8080
http://user:pass@proxy2.example.com:8080
socks5://proxy3.example.com:1080
192.168.1.100:8080
192.168.1.101:8080:admin:password
```

## 轮换策略

| 策略 | 说明 | rotation_interval |
|------|------|-------------------|
| `sequential` | 顺序循环 | ✅ 生效 |
| `random` | 每次随机选择 | ✅ 生效 |
| `random_no_repeat` | 打乱后依次，用完重新打乱 | ✅ 生效 |
| `per_account` | 同一账户映射到同一代理 | ❌ 不适用 |

### rotation_interval

- 仅对 `sequential`/`random`/`random_no_repeat` 策略生效
- 定时任务每 `rotation_interval` 秒执行一次轮换
- `get_proxy()` 返回当前全局代理

### per_account 策略

使用哈希映射 + 线性探测算法：

- **哈希映射**：`hash(organization_uuid) % len(proxies)` 计算基础索引
- **线性探测**：如果基础索引的代理不健康，顺序查找下一个健康代理
- **自动恢复**：代理恢复健康后，下次请求自动回到原始哈希位置
- **映射稳定**：只有代理列表（添加/删除/重排序）变化时映射才改变
- **无 account_id 时**：优先从 cookie 生成临时 ID（`cookie_{md5[:16]}`），否则使用 `fallback_strategy` 回退策略

## 健康管理

### 触发不健康的条件

| 条件 | 处理 |
|------|------|
| HTTP 层重试 3 次都失败（传输层异常） | 标记不健康，进入冷却期 |
| HTTP 403 + 使用代理 | 立即标记不健康，进入冷却期 |

### 不处理的情况

- HTTP 403 + 无代理 → 账户问题
- HTTP 429（限流） → 账户问题
- HTTP 401（认证失败） → 账户问题
- HTTP 500+（服务器错误） → 服务端问题

### 冷却恢复

- 冷却期由 `cooldown_duration` 配置（默认 1800 秒）
- 冷却期结束后自动恢复健康
- 在 `is_available` 属性中检查并自动恢复

## 异常处理

| 异常类 | 错误码 | HTTP 状态 | 说明 | 可重试 |
|-------|-------|---------|------|-------|
| `AllProxiesUnavailableError` | 503200 | 503 | 所有代理都在冷却期 | ✅ |
| `ProxyConnectionError` | 503201 | 503 | 代理连接失败 | ✅ |

异常响应中的代理信息会脱敏（仅保留 host:port，隐藏认证）。

## API 端点

### GET /api/admin/proxies

获取代理列表文件内容和解析数量。

**响应示例：**
```json
{
  "content": "http://proxy1:8080\nhttp://proxy2:8080",
  "count": 2
}
```

### PUT /api/admin/proxies

更新代理列表文件并重新加载。

**请求体：**
```json
{
  "content": "http://proxy1:8080\nhttp://proxy2:8080"
}
```

### GET /api/admin/proxies/status

获取代理池状态。

**响应示例：**
```json
{
  "mode": "dynamic",
  "total": 5,
  "available": 4,
  "current": "http://[auth]@proxy1:8080",
  "strategy": "sequential"
}
```

## 代理格式支持

```
# 完整 URL 格式
http://host:port
http://user:pass@host:port
https://host:port
socks5://host:port
socks5://user:pass@host:port

# 简化格式（默认 http）
host:port
host:port:user:pass    → 前两段是 host:port，剩余是 user:pass
user:pass:host:port    → 后两段是 host:port，前面是 user:pass
```

## 数据模型

### ProxyInfo 属性

| 属性 | 类型 | 说明 |
|------|------|------|
| `host` | str | 代理主机名/IP |
| `port` | int | 代理端口 |
| `username` | Optional[str] | 认证用户名 |
| `password` | Optional[str] | 认证密码 |
| `protocol` | str | 协议（http/https/socks5/socks5h） |
| `cooldown_until` | Optional[datetime] | 冷却结束时间，None 表示健康 |
| `url` | property | 完整代理 URL（含认证） |
| `url_safe` | property | 脱敏 URL（用于日志，隐藏认证） |
| `is_available` | property | 是否可用（健康或冷却期已满自动恢复） |
| `proxy_id` | property | 唯一标识符（protocol://host:port） |

## 架构

### 代理获取时机

```
代理获取时机：在 create_session() 调用前获取代理 URL

调用者 ──→ proxy_service.get_proxy(account_id) ──→ create_session(proxy=xxx)
```

| 调用位置 | Session 创建方式 | 代理获取时机 |
|----------|-----------------|--------------|
| ClaudeAPIProcessor | 每次请求新建 | 每次请求前获取 |
| ClaudeWebClient.initialize() | 会话初始化时创建 | 初始化时获取，会话内固定 |
| OAuth 服务 | 每次请求新建 | 每次请求前获取 |

### 故障处理流程

```
请求开始
    │
    ▼
get_proxy(account_id) → 返回代理 A（healthy）
    │
    ▼
HTTP 请求 ──失败──→ HTTP 层自动重试（同代理 A，最多 3 次）
    │                        │
    │                        ▼ 3 次都失败
    │                   标记 A 不健康，进入冷却期
    │                        │
    │                        ▼
    │                   抛出异常
    │                        │
    │                        ▼
    │              业务层重试（retryable=True）
    │                        │
    │                        ▼
    │              get_proxy() → 返回代理 B（A 已不健康被跳过）
    │                        │
    ▼                        ▼
成功               继续请求...
```

## 相关文件

| 文件 | 说明 |
|------|------|
| `app/models/proxy.py` | 代理数据模型（ProxyMode, RotationStrategy, ProxySettings, ProxyInfo） |
| `app/services/proxy.py` | 代理池服务（ProxyParser, ProxyPool） |
| `app/api/routes/proxies.py` | 代理列表 API |
| `app/core/config.py` | 配置中的 `proxy` 字段 |
| `app/core/exceptions.py` | 代理异常类（AllProxiesUnavailableError, ProxyConnectionError） |
| `app/core/http_client.py` | HTTP 客户端代理支持 |
| `front/src/components/DynamicProxySettings.tsx` | 前端代理设置组件 |

## TODO（后续迭代）

| 功能 | 说明 | 优先级 |
|------|------|--------|
| 主动健康检查 | 定期 TCP 测试代理连通性，当前仅被动检查 | 中 |
| SOCKS5 协议兼容性验证 | 代码已支持 socks5/socks5h，需补充测试 | 低 |
| 代理性能监控 | 延迟统计、成功率统计 | 低 |
| 时间+账户混合轮换策略 | 当前策略互斥，可考虑组合 | 低 |
| 前端代理列表可视化管理 | 当前仅文本编辑，可改为表格管理 | 中 |
