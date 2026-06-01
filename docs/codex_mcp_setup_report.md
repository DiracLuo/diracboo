# Codex MCP 挂载报告

> 日期：2026-05-31

## 配置结果

| 项目 | 值 |
|------|-----|
| Codex 可执行文件 | `/Applications/Codex.app/Contents/Resources/codex` |
| Codex 版本 | `codex-cli 0.135.0-alpha.1` |
| MCP server 名称 | `codex` |
| Transport 类型 | `stdio` |
| 启动命令 | `/Applications/Codex.app/Contents/Resources/codex mcp-server` |
| 配置作用域 | local（项目级，仅本项目可见） |
| MCP 状态 | ✓ Connected |

## 配置文件变更

- **修改**：`~/.claude.json`（新增 codex MCP server 配置）
- **未修改**：`~/.codex/config.toml`（保持原样）

## 验证结果

| 步骤 | 结果 |
|------|------|
| Codex CLI 路径 | ✅ 存在，版本 0.135.0-alpha.1 |
| mcp-server 子命令 | ✅ 支持 |
| MCP server 启动 | ✅ 无错误 |
| `claude mcp list` 显示 | ✅ Connected |
| 配置持久化 | ✅ 已写入 ~/.claude.json |
| 最小调用测试 | ⏳ 需要新会话（见下方说明） |

## 注意事项

MCP server 在本次会话中途添加，**Codex 工具在当前会话中不可用**。需要**重启 Claude Code 会话**后才能调用 Codex 工具。

重启后，Claude Code 应能看到以下工具（可能带 `mcp__codex__` 前缀）：
- `codex` — 启动新 Codex 会话
- `codex-reply` — 继续已有会话

## 后续使用方式

### 在新会话中测试
```
# 1. 启动新的 Claude Code 会话
claude

# 2. 调用 Codex 做只读分析（验证链路）
> 请 Codex 读取当前项目结构，概括项目用途，不要修改文件。

# 3. 调用 Codex 做代码任务
> 请 Codex 检查 alpha_ledger/screener.py 的筛选逻辑，提出优化建议。
```

### 协作模式
```
Claude Code（主控）
  ├─ 规划任务、写 spec
  ├─ 调用 mcp__codex__codex(prompt="按照 spec 实现...")
  ├─ 收到 Codex 返回结果
  ├─ Review 代码、跑测试
  └─ 反馈或继续
```

### 项目协作基础
- 同一项目目录：`/Users/dirac/code/Stock Analysis`
- 同一 git 仓库
- 共享文件：CLAUDE.md、AGENTS.md、README.md、docs/sync_codex_20260531.md
