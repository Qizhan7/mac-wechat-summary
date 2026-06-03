# 微信群聊 AI 总结

macOS 菜单栏应用，一键总结微信群聊消息。

直接读取本地微信数据库，调用 AI 生成群聊摘要，无需转发、无需截图。

[完整中文版 README](README.zh-CN.md)

![Python](https://img.shields.io/badge/Python-3.10+-blue) ![macOS](https://img.shields.io/badge/macOS-only-lightgrey) ![License](https://img.shields.io/badge/License-AGPL--3.0-blue)

## 功能

### 菜单栏应用 — 点一下就能用

装好之后菜单栏多一个小图标，选个群就能总结：

- **一键总结** — 选个群，几秒总结几百条消息
- **自定义范围** — 按时间段或条数总结，灵活控制
- **按天总结** — 下拉选年月日，总结指定某天的群聊消息；消息多时自动分段总结，保留更多细节
- **智能书签** — 自动记住上次读到哪，下次只总结新消息
- **分组批量** — 群聊可以分组，一键批量总结十几个同类群
- **跨群搜索** — 按关键词 + 日期范围搜索聊天记录，可选 AI 归纳分析
- **图片查看** — AI 可直接查看聊天图片和表情包，不再是一堆「[图片]」
- **ID 匿名化** — 无法识别真名的群成员自动显示为「成员1」「成员2」，避免 AI 被乱码 ID 干扰
- **关注推送** — 后台监控指定群聊，发现你关心的新功能、链接、实验报告或群内八卦时主动通知
- **链接展开** — 关注推送会尝试读取公开网页标题和摘要；微信聊天记录/收藏链接读不到正文时会明确标记，不让 AI 假装看过
- **Obsidian 知识库** — 命中内容自动沉淀成 Markdown，按稳定分类整理；笔记文件名和标题会带首次命中时间
- **总结历史** — 查看、复制往期总结

支持 Claude / DeepSeek / 通义千问 / ChatGPT / Ollama，不绑定任何一家。用 Ollama 可完全本地运行，数据不出电脑。

### 关注推送描述示例

在菜单栏「关注推送 → 设置关注描述」里可以写得具体一点，让模型知道什么该推、什么不该推。例如：

> 提醒我群里出现值得进一步了解的新功能、AI/产品新想法。优先通知有链接、有具体做法、明确产品/项目/模型名、实验结论、被多人认同、能启发实际项目的内容；只要出现“明确对象 + 新功能/更新/链接/教程/实验结果/修复方案/可执行做法”，即使只有单条也通知。也提醒能启发实际项目的 AI 互动实验、可玩玩法、模型行为边界与偏好测试，即使语气像玩笑也通知。多人附和的八卦和搞笑内容也推送；普通闲聊、只有情绪没有对象和信息量的内容不要通知。

命中后会写入本地知识库目录，默认是 `~/.wechat-summary/obsidian_knowledge/关注推送/`。文件名示例：`2026-05-29 03-16 Claude 4.8 发布传闻.md`。

关注推送会在判断前尝试展开消息里的公开网页链接，给 AI 补充标题和摘要。微信聊天记录、收藏记录等私有链接通常只能打开通用页面，程序会标记为「正文不可见」，再结合群聊上下文判断是否提醒。

### Obsidian 界面

关注推送知识库是本地 Markdown vault，不需要 Obsidian 插件或 API。想用 Obsidian 的搜索、反向链接、Graph、Canvas、Bases，只需要：

1. 安装 [Obsidian](https://obsidian.md/)
2. 在 Obsidian 里打开默认 vault：`~/.wechat-summary/obsidian_knowledge`
3. 如果已有自己的 vault，在菜单栏「关注推送 → 设置 Obsidian 仓库位置...」里填你的 vault 根目录

程序会给默认 vault 自动生成基础 `.obsidian` 配置、`首页.md` 和分类索引；如果你设置到自己的 vault，只会写入 `关注推送/` 子目录，不覆盖你的 Obsidian 界面配置。

### MCP Server — 让你的 AI Agent 直接读微信

接上 MCP Server 后，Claude Code、Cursor、OpenClaw 等 AI Agent 可以直接读取聊天记录，不需要截图发给 AI 做 OCR：

- 直接跟 AI 说「帮我看看工作群今天聊了啥」
- 「搜一下最近谁提过报销」
- 能看到图片和表情包，不再是一堆「[表情]」
- 还能帮你回微信，走微信界面操作，发送后自动验证是否成功

## 原理

不接入微信、不跑机器人，直接读取本地微信聊天记录，调用 AI 接口生成总结。

> 所有操作均在本地完成，聊天数据不经过第三方中转（仅发送给你选择的 AI 服务）。

## 安装

### 前置条件

- macOS 12+
- Python 3.10+
- 微信桌面版（已登录）
- 至少一个 AI 服务的 API Key（通义千问、DeepSeek 等）
- Obsidian 可选；只有想用图谱、反向链接和 vault 界面时需要安装

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
│   └── sender.py        # 消息发送（CGEvent UI 自动化）
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

> **消息发送：** 如需通过 AI Agent 发送微信消息，需在「系统设置 → 隐私与安全性 → 辅助功能」中授权运行 MCP Server 的应用（如终端、Claude.app）。发送使用 macOS 原生 UI 自动化，发送后会自动验证消息是否成功写入数据库。

> **注意：** 2026 年 3 月后微信更新了图片存储加密方式，新图片目前无法读取。之前的历史图片均可正常查看。

## 致谢

- [ylytdeng/wechat-decrypt](https://github.com/ylytdeng/wechat-decrypt) — 微信数据库解密方案参考，本项目在此基础上进行了改进和扩展
- [Obsidian](https://obsidian.md/) — 本地 Markdown vault 与知识图谱工作流灵感来源
- [Sue](https://github.com/smoonsue) — 测试与反馈

## License

[AGPL-3.0](LICENSE)
