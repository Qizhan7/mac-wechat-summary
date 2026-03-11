# 微信群聊 AI 总结

macOS 菜单栏应用，一键总结微信群聊消息。

直接读取本地微信数据库，调用 AI 生成群聊摘要，无需转发、无需截图。

![Python](https://img.shields.io/badge/Python-3.10+-blue) ![macOS](https://img.shields.io/badge/macOS-only-lightgrey) ![License](https://img.shields.io/badge/License-MIT-green)

## 功能

- **群聊总结** — 总结新消息 / 自定义条数或时间范围
- **分组管理** — 将多个群聊分组，支持批量总结
- **关键词搜索** — 按关键词 + 日期范围搜索聊天记录，可选 AI 归纳
- **总结历史** — 查看、复制往期总结
- **多 AI 服务** — 支持通义千问、DeepSeek、Ollama、Claude、OpenAI
- **MCP Server** — 支持通过 Claude Desktop / Claude Code 直接查询和总结微信消息

## 原理

读取本地微信聊天记录，调用 AI 接口生成总结。

> 所有操作均在本地完成，聊天数据不经过第三方中转（仅发送给你选择的 AI 服务）。

## 安装

### 前置条件

- macOS 12+
- Python 3.10+
- 微信桌面版（已登录）
- 至少一个 AI 服务的 API Key（通义千问、DeepSeek 等）

### 快速开始

1. 下载或 clone 本项目
2. 双击 `启动.command`，首次运行会自动安装依赖
3. 菜单栏出现 💬 图标后，点击设置 API Key
4. 选择群聊 → 总结新消息

详细说明见 [使用说明.txt](使用说明.txt) 和 [功能说明.txt](功能说明.txt)。

## 项目结构

```
├── app.py              # 主程序入口（菜单栏应用）
├── core/
│   ├── wechat_db.py    # 微信数据库解密与消息读取
│   └── key_extractor.py # 密钥提取管理
├── ai/
│   └── ai_service.py   # 多 AI 服务统一接口
├── ui/
│   └── dialogs.py      # GUI 对话框
├── c_src/
│   └── find_keys_macos.c # 内存扫描 C 程序
├── mcp_server.py       # MCP Server（供 Claude 调用）
├── 启动.command         # 一键启动脚本
├── first_run.sh        # 首次运行环境配置
├── setup.py            # 依赖安装脚本
└── requirements.txt    # Python 依赖
```

## MCP Server

支持通过 Claude Desktop 或 Claude Code 直接查询和总结微信消息。

在 Claude Desktop 配置文件中添加（`~/Library/Application Support/Claude/claude_desktop_config.json`）：

```json
{
  "mcpServers": {
    "wechat-summary": {
      "command": "/项目路径/.venv/bin/python3",
      "args": ["/项目路径/mcp_server.py"]
    }
  }
}
```

配置后可在 Claude 对话中查询群聊列表、读取聊天记录、总结消息和搜索关键词。

## 致谢

- [ylytdeng/wechat-decrypt](https://github.com/ylytdeng/wechat-decrypt) — 微信数据库解密方案参考，本项目在此基础上进行了改进和扩展
- [Sue](https://github.com/smoonsue) — 测试与反馈

## License

MIT
