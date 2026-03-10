#!/bin/bash
# 双击此文件即可启动微信总结
# 默认分发方式：保留整个源码目录，脚本会在本目录创建 .venv

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"

VENV_DIR="$PROJECT_DIR/.venv"
PYTHON_BIN="$VENV_DIR/bin/python"
REQ_FILE="$PROJECT_DIR/requirements.txt"
REQ_STAMP="$VENV_DIR/.requirements.sha256"
USER_DATA_DIR="$HOME/.wechat-summary"
SETUP_ONLY=0

if [[ "${1:-}" == "--setup-only" ]]; then
    SETUP_ONLY=1
fi

pause_and_exit() {
    local exit_code="$1"
    read -r -p "按回车键关闭..."
    exit "$exit_code"
}

get_wechat_app_path() {
    local app_path=""
    app_path="$(osascript -e 'POSIX path of (path to application "WeChat")' 2>/dev/null | tr -d '\r')"
    if [[ -n "$app_path" && -d "$app_path" ]]; then
        printf '%s\n' "${app_path%/}"
        return 0
    fi
    if [[ -d "/Applications/WeChat.app" ]]; then
        printf '%s\n' "/Applications/WeChat.app"
        return 0
    fi
    return 1
}

ensure_xcode_cli() {
    if xcode-select -p &>/dev/null; then
        return 0
    fi

    echo "需要安装 Xcode Command Line Tools，请在弹出窗口中点击「安装」"
    xcode-select --install
    echo ""
    echo "安装完成后，请再次双击「启动.command」"
    pause_and_exit 0
}

ensure_python() {
    if command -v python3 &>/dev/null; then
        return 0
    fi

    echo "❌ 未找到 Python3，请先安装："
    echo "   1. 去 python.org 下载 Python 3.9 以上版本"
    echo "   2. 安装完成后重新双击「启动.command」"
    pause_and_exit 1
}

ensure_venv() {
    mkdir -p "$USER_DATA_DIR"

    if [[ ! -x "$PYTHON_BIN" ]]; then
        echo "[1/3] 创建项目隔离环境..."
        python3 -m venv "$VENV_DIR"
    fi

    local current_hash=""
    local installed_hash=""
    current_hash="$(shasum -a 256 "$REQ_FILE" | awk '{print $1}')"
    if [[ -f "$REQ_STAMP" ]]; then
        installed_hash="$(cat "$REQ_STAMP")"
    fi

    if [[ "$current_hash" != "$installed_hash" ]]; then
        echo "[1/3] 安装 Python 依赖（首次需要几分钟）..."
        "$PYTHON_BIN" -m pip install --upgrade pip
        "$PYTHON_BIN" -m pip install -r "$REQ_FILE"
        printf '%s\n' "$current_hash" > "$REQ_STAMP"
    else
        echo "[1/3] Python 依赖已就绪"
    fi
}

is_wechat_signed() {
    local app_path="$1"
    local codesign_output=""

    if ! codesign_output="$(codesign -dvv "$app_path" 2>&1)"; then
        return 1
    fi

    if printf '%s\n' "$codesign_output" | grep -qi "runtime"; then
        return 1
    fi

    return 0
}

quit_wechat_if_running() {
    if ! pgrep -x "WeChat" &>/dev/null; then
        return 0
    fi

    echo "  检测到微信正在运行，正在退出..."
    osascript -e 'tell application "WeChat" to quit' 2>/dev/null || true
    sleep 2
    if pgrep -x "WeChat" &>/dev/null; then
        killall WeChat 2>/dev/null || true
        sleep 1
    fi
    echo "  ✓ 微信已退出"
}

ensure_wechat_signed() {
    local app_path=""
    if ! app_path="$(get_wechat_app_path)"; then
        echo "[2/3] ❌ 未找到 WeChat.app，请先安装并登录微信"
        pause_and_exit 1
    fi

    if is_wechat_signed "$app_path"; then
        echo "[2/3] 微信授权状态正常"
        return 0
    fi

    echo "[2/3] 检测到微信需要重新授权..."
    echo "  微信更新后会自动失效，这一步会重新处理"
    echo "  需要输入电脑登录密码；输入时终端不会显示字符，这是正常的"
    quit_wechat_if_running

    if sudo codesign --force --deep --sign - "$app_path"; then
        echo "  ✓ 微信已重签名"
        return 0
    fi

    echo ""
    echo "============================================"
    echo "  ❌ 微信重新授权失败"
    echo "============================================"
    echo ""
    echo "请按下面步骤处理后，再重新双击「启动.command」："
    echo "  1. 打开「系统设置」"
    echo "  2. 进入「隐私与安全性」"
    echo "  3. 找到「App 管理」或「完全磁盘访问权限」"
    echo "  4. 打开「终端」的开关"
    echo "  5. 重新运行本脚本"
    pause_and_exit 1
}

run_setup() {
    if [[ "$(uname)" != "Darwin" ]]; then
        echo "❌ 此工具仅支持 macOS"
        pause_and_exit 1
    fi

    echo "============================================"
    if [[ "$SETUP_ONLY" -eq 1 ]]; then
        echo "  微信群聊 AI 总结 - 环境检查"
    else
        echo "  微信群聊 AI 总结"
    fi
    echo "============================================"
    echo ""

    ensure_xcode_cli
    ensure_python
    ensure_venv
    ensure_wechat_signed

    echo "[3/3] 环境已就绪"
    echo ""
}

run_setup

if [[ "$SETUP_ONLY" -eq 1 ]]; then
    echo "配置完成。后续直接双击「启动.command」即可。"
    pause_and_exit 0
fi

# ── 修复密钥：C 扫描器以 root 运行无法读取 sandbox 文件，
#    用 Python（用户权限）重新匹配 ──
"$PYTHON_BIN" -c "
import os, json, sys
DATA = os.path.expanduser('~/.wechat-summary')
log_f  = os.path.join(DATA, 'extract_keys.log')
keys_f = os.path.join(DATA, 'all_keys.json')
cfg_f  = os.path.join(DATA, 'config.json')
if not os.path.exists(log_f) or not os.path.exists(cfg_f):
    sys.exit()
# 检查 all_keys.json 是否已有内容
try:
    ks = {k:v for k,v in json.load(open(keys_f)).items() if not k.startswith('_')}
    if ks: sys.exit()
except: pass
# 从日志解析 key+salt
raw = []
for line in open(log_f):
    p = line.split()
    if len(p)>=3 and len(p[-2])==64 and len(p[-1])==32:
        try: bytes.fromhex(p[-2]); bytes.fromhex(p[-1]); raw.append((p[-2].lower(),p[-1].lower()))
        except: pass
if not raw: sys.exit()
# 匹配 DB 文件头 salt
s2k = {s:k for k,s in raw}
db_dir = json.load(open(cfg_f)).get('db_dir','')
if not db_dir or not os.path.isdir(db_dir): sys.exit()
matched = {}
for root,_,files in os.walk(db_dir):
    for fn in files:
        if not fn.endswith('.db'): continue
        fp = os.path.join(root, fn)
        try:
            h = open(fp,'rb').read(16)
            if len(h)<16 or h[:15]==b'SQLite format 3': continue
            salt = h.hex().lower()
            if salt in s2k: matched[os.path.relpath(fp,db_dir)] = {'enc_key': s2k[salt]}
        except: pass
if matched:
    try: os.remove(keys_f)
    except: pass
    with open(keys_f,'w') as f: json.dump(matched,f,indent=2)
    print(f'[fix] 自动修复了 {len(matched)} 个数据库密钥')
" 2>/dev/null || true

echo "正在启动微信总结..."
echo "菜单栏会出现 💬 图标"
echo "（关闭此窗口会退出程序，Ctrl+C 也可退出）"
echo ""
exec "$PYTHON_BIN" "$PROJECT_DIR/app.py"
