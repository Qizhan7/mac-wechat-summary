"""py2app 打包配置（可选；默认仍推荐源码目录 + 启动.command 分发）"""
from setuptools import setup

APP = ["app.py"]
OPTIONS = {
    "argv_emulation": False,
    "plist": {
        "CFBundleName": "微信总结",
        "CFBundleShortVersionString": "1.0.0",
        "LSUIElement": True,  # 不在 Dock 显示图标
    },
    "packages": [
        "rumps",
        "Crypto",
        "zstandard",
        "anthropic",
        "openai",
        "requests",
        "objc",
        "ai",
        "core",
        "ui",
    ],
    "resources": ["c_src", "使用说明.txt"],
}

setup(
    app=APP,
    name="WeChatSummary",
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
    install_requires=[
        "rumps>=0.4.0",
        "pycryptodome>=3.20.0",
        "zstandard>=0.22.0",
        "pyobjc-framework-Cocoa>=10.0",
        "anthropic>=0.40.0",
        "openai>=1.50.0",
        "requests>=2.31.0",
        "mcp[cli]>=1.0.0",
    ],
)
