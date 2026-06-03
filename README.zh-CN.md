# 微信群聊 AI 总结

一个 macOS 菜单栏工具，用来读取本地微信聊天记录，生成群聊总结、关键词搜索结果，并把值得关注的新功能/链接/实验结论自动沉淀到 Obsidian 风格知识库。

它不接入微信接口，不跑机器人，也不需要把聊天截图转发给 AI。程序直接读取你电脑上的微信数据库副本，调用你自己配置的 AI 服务生成结果。

![Python](https://img.shields.io/badge/Python-3.10+-blue)
![macOS](https://img.shields.io/badge/macOS-only-lightgrey)
![License](https://img.shields.io/badge/License-AGPL--3.0-blue)

## 适合谁

- 想快速回顾微信群聊重点，不想翻几百条消息的人
- 想每天批量总结工作群、学习群、项目群的人
- 想让 Claude Code、Cursor、OpenClaw 等 AI Agent 直接查询微信记录的人
- 想监控群里出现的新功能、教程、实验报告、产品想法，并自动整理进 Obsidian 的人

## 功能概览

### 菜单栏总结

- **总结新消息**：自动从上次读到的位置继续总结，不重复处理旧消息
- **自定义范围**：按最近 N 条或最近 N 分钟总结
- **按天总结**：选择年月日，回顾某一天的群聊记录
- **总结历史**：菜单中查看、复制和打开近期总结文件
- **智能书签**：每个群聊单独记录已读位置

### 群聊管理

- **群聊列表刷新**：读取最近活跃群聊
- **分组管理**：把多个群聊放进工作、学习、项目等分组
- **分组批量总结**：一个分组内多个群聊一键生成综合报告
- **显示群昵称**：优先显示备注名/群名片，减少乱码 ID 干扰

### 搜索与图片

- **跨群关键词搜索**：按关键词和日期范围搜索多个群聊
- **AI 搜索总结**：可让 AI 把搜索结果整理成摘要
- **图片查看**：历史图片和表情包可交给 AI 理解，不再只显示 `[图片]`

> 注意：2026 年 3 月后微信更新了图片存储加密方式，新图片目前可能无法读取；此前历史图片仍可正常查看。

### 关注推送与 Obsidian 知识库

- **关注推送**：后台监控指定群聊，出现你关心的新功能、链接、教程、实验结果、八卦等内容时通知你
- **知识库沉淀**：命中内容自动写入本地 SQLite 知识库
- **Markdown 导出**：自动导出到 Obsidian-friendly Markdown vault
- **时间标题**：文件名和标题都带首次命中日期时间，清扫后也能看出来源时间
- **清扫整理**：合并近似重复主题、归并分类文件夹、重新导出笔记
- **关系链接**：新线索、反转、相关主题会生成 Obsidian 双链

### MCP Server

接入 MCP Server 后，Claude Desktop、Claude Code、Cursor、OpenClaw 等 AI Agent 可以直接：

- 查询群聊列表
- 读取指定群聊消息
- 搜索聊天记录
- 总结某个时间段的群聊
- 查看聊天图片和表情包
- 通过微信界面发送消息，并自动验证是否发送成功

## 隐私与安全

- 程序只读取微信数据库副本，不修改微信文件
- 聊天内容只发送给你选择的 AI 服务，不经过项目作者服务器
- 使用 Ollama 本地模型时，消息内容可以完全不离开本机
- API Key 存储在 macOS 钥匙串中，不明文写入配置文件
- MCP 发送微信消息使用 macOS 原生 UI 自动化，需要你主动授权辅助功能权限

## 安装

### 前置条件

- macOS 12 或更高版本
- Python 3.10 或更高版本
- 微信桌面版，且已经登录
- 至少一个 AI 服务 API Key
- Obsidian 可选；只有想用图谱、反向链接、Canvas、Bases 或 vault 界面时需要安装

支持的 AI 服务：

- 通义千问
- DeepSeek
- Claude
- OpenAI
- Ollama 本地模型

### 快速开始

1. 下载或 clone 本项目。
2. 双击 `启动.command`。
3. 首次运行时等待自动安装依赖、创建 `.venv`、检查微信授权。
4. 菜单栏出现图标后，打开「设置」，填入 AI 服务和 API Key。
5. 选择群聊，点击「总结新消息」。

如果 macOS 阻止运行 `启动.command`，右键它，选择「打开」，再在弹窗中确认打开。第一次这样做即可。

### 命令行启动

也可以在项目目录运行：

```bash
./启动.command
```

首次启动会自动创建项目自己的 Python 虚拟环境 `.venv/`，不会污染系统 Python。

## 日常使用

### 总结群聊

1. 确保微信已启动并登录。
2. 启动本项目。
3. 点击菜单栏图标。
4. 选择群聊。
5. 点击「总结新消息」「自定义总结」或「按天总结」。

总结文件会保存到：

```text
~/.wechat-summary/
```

### 关键词搜索

在菜单中点击「关键词搜索」，填写：

- 关键词：多个关键词用空格分隔
- 开始日期：格式为 `YYYY-MM-DD`
- 结束日期：可选，留空则到今天
- 群聊范围：全部群聊或指定群聊
- AI 总结：可选择是否让 AI 归纳搜索结果

搜索是只读操作，不会影响书签或总结状态。

### 分组批量总结

可以把常看的群聊放入分组，例如「工作群」「AI 群」「项目群」。进入分组后点击「一键总结」，程序会批量读取这些群聊的新消息，生成一份综合总结。

## 关注推送

关注推送适合用来盯「不是每条都重要，但错过又可惜」的信息。

### 设置方式

1. 菜单栏点击「关注推送」。
2. 点击「设置关注描述」。
3. 写下你想监控什么内容。
4. 选择要监控的群聊。
5. 开启关注推送。

推荐写得具体一点，例如：

> 提醒我群里出现值得进一步了解的新功能、AI/产品新想法。优先通知有链接、有具体做法、明确产品/项目/模型名、实验结论、被多人认同、能启发实际项目的内容；只要出现“明确对象 + 新功能/更新/链接/教程/实验结果/修复方案/可执行做法”，即使只有单条也通知。多人附和的八卦和搞笑内容也推送；普通闲聊、只有情绪没有对象和信息量的内容不要通知。

### 知识库位置

默认知识库数据库：

```text
~/.wechat-summary/monitor_knowledge.db
```

默认 Obsidian Markdown vault：

```text
~/.wechat-summary/obsidian_knowledge/
```

命中笔记默认写入：

```text
~/.wechat-summary/obsidian_knowledge/关注推送/
```

文件名示例：

```text
2026-05-29 03-16 Claude 4.8 发布传闻.md
```

## Obsidian 界面

关注推送知识库是普通 Markdown vault，不需要安装 Obsidian 插件，也不依赖 Obsidian API。

### 使用默认 vault

1. 安装 [Obsidian](https://obsidian.md/)。
2. 打开 Obsidian。
3. 选择「打开本地仓库」。
4. 选择：

```text
~/.wechat-summary/obsidian_knowledge
```

程序会给默认 vault 自动生成：

- 基础 `.obsidian` 配置
- `首页.md`
- 分类索引
- `关注推送/` 分类目录

### 使用自己的 vault

如果你已经有自己的 Obsidian vault：

1. 菜单栏点击「关注推送」。
2. 点击「设置 Obsidian 仓库位置...」。
3. 填入你的 vault 根目录。

程序只会在你的 vault 下写入 `关注推送/` 子目录，不覆盖你的 `.obsidian` 界面配置。

## MCP Server 配置

以 Claude Desktop 为例，在配置文件中添加：

```text
~/Library/Application Support/Claude/claude_desktop_config.json
```

配置示例：

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

其他兼容 MCP 协议的客户端，如 Claude Code、Cursor、OpenClaw，也可以用类似方式配置。

如需让 AI Agent 发送微信消息，需要在 macOS 中授权运行 MCP Server 的应用：

```text
系统设置 → 隐私与安全性 → 辅助功能
```

授权后，AI 会通过微信界面发送消息，并在数据库中验证最近消息，确认发送是否成功。

## 常见问题

### 提示 Python 版本太低怎么办？

需要 Python 3.10 或更高版本。启动脚本会尝试自动检测和处理；如果自动安装失败，可以手动安装 Python 3.12。

### macOS 提示文件无法打开怎么办？

右键 `启动.command`，选择「打开」，再在弹窗中点击「打开」。这是 macOS 对下载文件的正常保护。

### 输入密码时看不到字符，是不是卡住了？

不是。macOS 终端输入管理员密码时不会显示字符，也不会显示星号。直接输入电脑登录密码并回车即可。

### 微信更新后读不到聊天记录怎么办？

重新运行 `启动.command`。脚本会检查微信授权状态，并在需要时提示处理。

### 关闭终端窗口会怎样？

如果是双击 `启动.command` 启动的，关闭对应终端窗口会退出菜单栏程序。

### 数据保存在哪里？

主要数据目录：

```text
~/.wechat-summary/
```

项目虚拟环境：

```text
项目目录/.venv/
```

配置文件：

```text
~/.wechat-summary/config.json
```

## 项目结构

```text
├── app.py                # 菜单栏应用入口
├── core/
│   ├── wechat_db.py      # 微信数据库读取
│   ├── decryptor.py      # SQLCipher 数据库解密
│   ├── key_extractor.py  # 密钥提取
│   ├── config.py         # 配置管理
│   ├── keychain.py       # macOS 钥匙串
│   ├── bookmark.py       # 阅读书签
│   ├── chat_groups.py    # 群聊分组
│   ├── knowledge.py      # 关注推送知识库与 Obsidian 导出
│   └── sender.py         # 微信消息发送 UI 自动化
├── ai/                   # AI provider
├── c_src/                # 微信密钥扫描辅助程序
├── resources/            # 菜单栏图标资源
├── mcp_server.py         # MCP Server
├── 启动.command           # 一键启动脚本
├── 使用说明.txt           # 面向普通用户的安装说明
├── 功能说明.txt           # 功能详解
└── requirements.txt
```

## 开发与测试

创建依赖环境后，可以运行：

```bash
.venv/bin/python -m unittest test_monitor.py test_knowledge.py
.venv/bin/python -m py_compile app.py core/monitor.py core/knowledge.py core/config.py core/wechat_db.py distill_me.py extract_my_style.py
```

## 致谢

- [ylytdeng/wechat-decrypt](https://github.com/ylytdeng/wechat-decrypt) — 微信数据库解密方案参考，本项目在此基础上进行了改进和扩展
- [Obsidian](https://obsidian.md/) — 本地 Markdown vault 与知识图谱工作流灵感来源
- [Sue](https://github.com/smoonsue) — 测试与反馈

## License

本项目使用 [AGPL-3.0](LICENSE) 协议。
