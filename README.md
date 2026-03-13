# 微信群聊 AI 总结

macOS 菜单栏应用，一键总结微信群聊消息。

直接读取本地微信数据库，调用 AI 生成群聊摘要，无需转发、无需截图。

![Python](https://img.shields.io/badge/Python-3.10+-blue) ![macOS](https://img.shields.io/badge/macOS-only-lightgrey) ![License](https://img.shields.io/badge/License-MIT-green)

## 功能

### 菜单栏应用 — 点一下就能用

装好之后菜单栏多一个小图标，选个群就能总结：

- **一键总结** — 选个群，几秒总结几百条消息
- **自定义范围** — 按时间段或条数总结，灵活控制
- **智能书签** — 自动记住上次读到哪，下次只总结新消息
- **分组批量** — 群聊可以分组，一键批量总结十几个同类群
- **跨群搜索** — 按关键词 + 日期范围搜索聊天记录，可选 AI 归纳分析
- **图片查看** — AI 可直接查看聊天图片和表情包，不再是一堆「[图片]」
- **总结历史** — 查看、复制往期总结

支持 Claude / DeepSeek / 通义千问 / ChatGPT / Ollama，不绑定任何一家。用 Ollama 可完全本地运行，数据不出电脑。

### MCP Server — 让你的 AI Agent 直接读微信

接上 MCP Server 后，Claude Code、Cursor、OpenClaw 等 AI Agent 可以直接读取聊天记录，不需要截图发给 AI 做 OCR：

- 直接跟 AI 说「帮我看看工作群今天聊了啥」
- 「搜一下最近谁提过报销」
- 能看到图片和表情包，不再是一堆「[表情]」
- 还能帮你回微信，走微信界面操作，全过程可见

## 原理

不接入微信、不跑机器人，直接读取本地微信聊天记录，调用 AI 接口生成总结。

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
3. 菜单栏出现 <img src="resources/icon.png" width="16" height="16"> 图标后，点击设置 API Key
4. 选择群聊 → 总结新消息

详细说明见 [使用说明.txt](使用说明.txt) 和 [功能说明.txt](功能说明.txt)。

## 项目结构

```
├── app.py               # 主程序入口（菜单栏应用）
├── core/
│   ├── wechat_db.py     # 微信数据库解密与消息读取
│   ├── decryptor.py     # SQLCipher 数据库解密
│   ├── key_extractor.py # 密钥提取管理
│   ├── config.py        # 配置管理
│   ├── keychain.py      # macOS 钥匙串存取
│   ├── bookmark.py      # 阅读书签
│   ├── chat_groups.py   # 分组管理
│   └── sender.py        # 消息发送
├── ai/
│   ├── base.py          # AI 提供者基类与 Prompt
│   ├── factory.py       # AI 提供者工厂
│   ├── claude_provider.py
│   ├── openai_provider.py
│   └── ollama_provider.py
├── c_src/
│   └── find_keys_macos.c # 内存扫描 C 程序（运行时自动编译）
├── resources/            # 菜单栏图标资源
├── mcp_server.py        # MCP Server（供 AI Agent 调用）
├── 启动.command          # 一键启动脚本（双击即可运行）
├── setup.py             # py2app 打包配置（可选）
├── requirements.txt     # Python 依赖
├── 使用说明.txt          # 安装与使用指南
└── 功能说明.txt          # 功能详细说明
```

## MCP Server 配置

以 Claude Desktop 为例，在配置文件中添加（`~/Library/Application Support/Claude/claude_desktop_config.json`）：

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

其他兼容 MCP 协议的客户端（Cursor、Claude Code 等）配置方式类似。

> **注意：** 2026 年 3 月后微信更新了图片存储加密方式，新图片目前无法读取。之前的历史图片均可正常查看。

## 致谢

- [ylytdeng/wechat-decrypt](https://github.com/ylytdeng/wechat-decrypt) — 微信数据库解密方案参考，本项目在此基础上进行了改进和扩展
- [Sue](https://github.com/smoonsue) — 测试与反馈

## License

MIT
