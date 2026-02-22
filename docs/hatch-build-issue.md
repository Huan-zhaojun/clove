# Hatch 构建配置问题

## 问题

`uv sync` 安装依赖时报错：

```
FileNotFoundError: Forced include not found: C:\program\AI\clove\app\static
```

## 原因

`pyproject.toml` 中的 `force-include` 配置：

```toml
[tool.hatch.build.targets.wheel.force-include]
"app/static" = "app/static"
"app/locales" = "app/locales"
```

该配置本意是构建 wheel 时包含前端静态文件，但 `hatchling` 在开发模式安装（editable install）时也会检查这些路径。

## 影响

| 阶段 | 期望 | 实际 |
|------|------|------|
| 开发 `uv sync` | 不需要 static | ❌ 报错 |
| 构建 `uv build` | 需要 static | ✅ 正常 |

## 临时解决

手动创建空目录：

```bash
mkdir -p app/static app/locales
```

## 待优化

修改构建配置，让 `force-include` 只在实际构建时生效，开发阶段不强制要求。