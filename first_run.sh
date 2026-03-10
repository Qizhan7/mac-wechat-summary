#!/bin/bash
# 兼容旧入口：只做环境检查，不直接启动应用

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"

exec bash "$PROJECT_DIR/启动.command" --setup-only
