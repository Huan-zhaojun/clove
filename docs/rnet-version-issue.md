# rnet 使用指南

本文档记录 clove 项目中 rnet 库的使用经验和注意事项。

---

## Proxy 方法说明（重要！）

### 方法行为（经源码确认）

通过分析 [wreq 源码](https://github.com/0x676e67/wreq/blob/main/src/proxy.rs)，确认各方法的实际行为：

| 方法 | 实际行为 |
|------|---------|
| `Proxy.http(url)` | **只拦截 `http://` 请求**，HTTPS 请求不会走代理！ |
| `Proxy.https(url)` | **只拦截 `https://` 请求**，HTTP 请求不会走代理 |
| `Proxy.all(url)` | **拦截所有请求** ✅ 正确用法 |

### 正确用法

```python
# ✅ 正确：使用 Proxy.all() 拦截所有请求
proxies = [rnet.Proxy.all(url=proxy)]

# ❌ 错误：Proxy.http() 不会拦截 HTTPS 请求！
proxies = [rnet.Proxy.http(url=proxy)]
```

### wreq 源码证据

```rust
// wreq/src/proxy/matcher.rs
pub fn intercept(&self, dst: &Uri) -> Option<Intercepted> {
    if dst.is_http() {
        return self.http.clone().map(Intercepted::Proxy);  // 只匹配 http://
    }
    if dst.is_https() {
        return self.https.clone().map(Intercepted::Proxy); // 只匹配 https://
    }
    None
}
```

### 勘误：之前的错误测试（2025-12）

**之前的测试结论是错误的！**

错误过程：
1. 使用 `Proxy.http()` 测试访问 HTTPS 目标（claude.ai）
2. 本机同时运行了 Clash TUN 模式
3. `Proxy.http()` 不拦截 HTTPS 请求，请求直接走了本机网络
4. Clash TUN 拦截了本机网络流量并转发
5. 测试"成功"，误以为 `Proxy.http()` 能处理 HTTPS 请求

**教训**：测试代理时务必关闭本机的 TUN/VPN 代理，否则结果会被干扰。

---

## rnet 3.x 升级记录（2025-12）

### 分支

`chore/upgrade-rnet-v3.0.0-rc14`

### 修改的文件

| 文件 | 修改内容 |
|------|----------|
| `pyproject.toml` | `rnet>=2.3.9` → `rnet>=3.0.0rc14` |
| `app/core/http_client.py` | rnet 3.x API 适配 |
| `uv.lock` | 依赖锁定文件更新 |

### API 变更

#### 1. 浏览器指纹枚举

```python
# rnet 2.x
from rnet import Impersonate
client = rnet.Client(impersonate=Impersonate.Chrome136)

# rnet 3.x
from rnet import Emulation
client = rnet.Client(emulation=Emulation.Chrome142)
```

#### 2. 状态码获取

```python
# rnet 2.x
status_code = response.status  # 返回 int

# rnet 3.x
status_code = response.status.as_int()  # StatusCode 对象需要转换
```

#### 3. 响应头迭代

```python
# rnet 2.x
for key, value in response.headers.items():
    ...

# rnet 3.x（HeaderMap 没有 items() 方法）
for key, value in response.headers:
    ...
```

#### 4. 代理构建方式

```python
# rnet 2.x
proxies = [rnet.Proxy.all(proxy)]

# rnet 3.x（必须使用命名参数）
proxies = [rnet.Proxy.all(url=proxy)]
```

#### 5. 超时参数

```python
# rnet 2.x
client = RnetClient(connect_timeout=timeout, ...)

# rnet 3.x
client = RnetClient(timeout=timeout, ...)
```

---

## 测试脚本

位置：`tests/test_proxy_methods.py`

```bash
# 运行测试（确保关闭本机 TUN/VPN 代理以获得准确结果）
uv run python tests/test_proxy_methods.py
```

---

## 相关文件

- `pyproject.toml` - 依赖配置
- `app/core/http_client.py` - HTTP 客户端实现
- `tests/test_proxy_methods.py` - 代理测试脚本

## 参考

- [rnet GitHub](https://github.com/0x676e67/rnet)
- [wreq GitHub](https://github.com/0x676e67/wreq)（rnet 底层 Rust 库）
