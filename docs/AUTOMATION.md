# Alpha Ledger 自动化定时任务

## 概述

生产日报通过 macOS launchd 定时任务自动运行，每个工作日 16:30 CST 触发。

脚本会自动判断是否为 A 股交易日（排除周末和数据库中无交易记录的日期），非交易日跳过。

## 文件位置

| 文件 | 路径 |
|------|------|
| 包装脚本 | `scripts/daily_production.sh` |
| launchd 配置 | `scripts/com.alphaledger.daily.plist` |
| 安装位置 | `~/Library/LaunchAgents/com.alphaledger.daily.plist` |
| 生产日志 | `logs/daily_production_<date>.log` |
| launchd 标准输出 | `logs/launchd_stdout.log` |
| launchd 错误输出 | `logs/launchd_stderr.log` |

## 常用命令

```bash
# 查看定时任务状态
launchctl list | grep alphaledger

# 查看当天运行日志
cat "/Users/dirac/code/Stock Analysis/logs/daily_production_$(date +%Y-%m-%d).log"

# 查看 launchd 输出日志
cat "/Users/dirac/code/Stock Analysis/logs/launchd_stdout.log"

# 手动触发一次（测试用）
bash "/Users/dirac/code/Stock Analysis/scripts/daily_production.sh"

# 暂停定时任务（不删除配置）
launchctl unload ~/Library/LaunchAgents/com.alphaledger.daily.plist

# 恢复定时任务
launchctl load ~/Library/LaunchAgents/com.alphaledger.daily.plist

# 彻底卸载
launchctl unload ~/Library/LaunchAgents/com.alphaledger.daily.plist
rm ~/Library/LaunchAgents/com.alphaledger.daily.plist
```

## 执行流程

定时任务触发后，脚本按以下顺序执行：

1. **周末检查**：周六/周日直接跳过
2. **交易日检查**：查询数据库交易日历，非交易日跳过
3. **production-run**：数据更新 → 断点修复 → 审计 → Qlib 刷新 → 模型预测 → 分时复核 → 生成日报
4. **production-async**：异步补充事件、财报、资金流等慢数据

## 注意事项

- 定时任务使用系统本地时间，确保 Mac 时区设为 Asia/Shanghai
- 代码修改会立即生效，无需重新安装定时任务（Python 运行时动态加载模块）
- 如果 Mac 在 16:30 处于睡眠状态，任务会在唤醒后补执行
- 网络故障导致的失败会记录在日志中，不会自动重试
