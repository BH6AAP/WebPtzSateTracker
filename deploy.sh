#!/usr/bin/env bash
# ============================================================
# YD3040 云台 · 卫星跟踪系统 —— Linux 一键部署脚本
# 功能: 同步后端/前端代码到服务器 -> 安装依赖 -> 重启服务 -> 验证
# 用法: bash deploy.sh
# ============================================================
set -euo pipefail

# ---------- 服务器配置 (通过环境变量注入, 勿硬编码密码) ----------
# 用法: PTZ_HOST=192.168.31.67 PTZ_USER=aap PTZ_PASS=xxx bash deploy.sh
HOST="${PTZ_HOST:-192.168.31.67}"
USER="${PTZ_USER:-aap}"
PASS="${PTZ_PASS:-}"
REMOTE_DIR="/home/aap/ptz"
SERVICE="ptz"

# 未提供密码则提示
if [ -z "$PASS" ]; then
    read -rsp "请输入服务器 SSH 密码: " PASS
    echo
fi

# ---------- 本地项目路径 ----------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$SCRIPT_DIR/backend"
FRONTEND_DIR="$SCRIPT_DIR/frontend"

# ---------- 颜色输出 ----------
GREEN='\033[0;32m'; RED='\033[0;31m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[部署]${NC} $1"; }
warn()  { echo -e "${YELLOW}[警告]${NC} $1"; }
fail()  { echo -e "${RED}[错误]${NC} $1"; exit 1; }

# ---------- 检查依赖 ----------
command -v sshpass >/dev/null 2>&1 || fail "缺少 sshpass, 请先安装: brew install sshpass (macOS) / apt install sshpass (Linux)"
command -v ssh >/dev/null 2>&1 || fail "缺少 ssh"

SSH="sshpass -p $PASS ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 $USER@$HOST"
SCP="sshpass -p $PASS scp -o StrictHostKeyChecking=no"

# ---------- 1. 连通性检查 ----------
info "检查服务器连通性: $USER@$HOST ..."
$SSH "echo ok" >/dev/null 2>&1 || fail "无法连接服务器, 请检查网络/IP/账号"

# ---------- 2. 同步后端代码 ----------
info "同步后端代码 -> $REMOTE_DIR/backend/"
$SCP "$BACKEND_DIR/main.py" "$BACKEND_DIR/auth.py" "$BACKEND_DIR/satellite.py" "$BACKEND_DIR/streaming.py" \
     "$BACKEND_DIR/pelco.py" "$BACKEND_DIR/rotctld_server.py" "$BACKEND_DIR/ws_server.py" "$BACKEND_DIR/requirements.txt" \
     "$BACKEND_DIR/lotw.py" \
     "$USER@$HOST:$REMOTE_DIR/backend/"

# ---------- 3. 同步前端静态文件 ----------
info "同步前端文件 -> $REMOTE_DIR/frontend/"
$SCP "$FRONTEND_DIR/index.html" "$FRONTEND_DIR/satellite.js" "$FRONTEND_DIR/earth_diffuse.jpg" \
     "$USER@$HOST:$REMOTE_DIR/frontend/"

# ---------- 4. 安装依赖 (仅 requirements 变化时) ----------
info "检查并安装 Python 依赖 ..."
$SSH "cd $REMOTE_DIR/backend && $REMOTE_DIR/venv/bin/pip install -q -r requirements.txt 2>/dev/null || echo '依赖安装跳过/失败'" || true

# 4.1 确保 sgp4 为 C 加速版 (纯 Python 版慢几十倍; C 扩展为 vallado_cpp*.so)
info "确认 sgp4 C 加速版 ..."
SGP4_CHECK='import glob,os,sgp4; d=os.path.dirname(sgp4.__file__); exit(0 if glob.glob(os.path.join(d,"*.so")) else 1)'
if $SSH "$REMOTE_DIR/venv/bin/python -c '$SGP4_CHECK'" 2>/dev/null; then
    info "sgp4 C 加速已就绪 ✓"
else
    warn "sgp4 缺少 C 加速, 尝试升级安装 ..."
    $SSH "$REMOTE_DIR/venv/bin/pip install -q -U sgp4" \
        && $SSH "$REMOTE_DIR/venv/bin/python -c '$SGP4_CHECK'" \
        && info "sgp4 C 加速安装成功 ✓" \
        || warn "sgp4 C 加速安装失败, 性能将受影响"
fi

# ---------- 5. 重启服务 ----------
info "重启 $SERVICE 服务 ..."
$SSH "echo '$PASS' | sudo -S systemctl restart $SERVICE 2>/dev/null && sleep 3"

# ---------- 6. 验证 ----------
info "验证服务状态 ..."
STATUS=$($SSH "systemctl is-active $SERVICE" 2>/dev/null || echo "unknown")
if [ "$STATUS" = "active" ]; then
    info "服务状态: active ✓"
else
    warn "服务状态: $STATUS (可能启动失败, 请查看日志: journalctl -u $SERVICE -f)"
fi

# 验证 HTTP 接口
HTTP_CODE=$($SSH "curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8090/api/auth/me" 2>/dev/null || echo "000")
info "HTTP 接口 /api/auth/me 返回: $HTTP_CODE (401=正常, 需登录)"

# 验证 UDP 编码器监听
UDP_OK=$($SSH "ss -ulnp | grep -q 8091 && echo yes || echo no" 2>/dev/null || echo "no")
if [ "$UDP_OK" = "yes" ]; then
    info "UDP 编码器监听 8091: 正常 ✓"
else
    warn "UDP 编码器监听 8091: 未检测到"
fi

info "部署完成!"
echo "--------------------------------------------------"
echo "  本地访问:  http://$HOST:8090"
echo "  外网访问:  https://ptz.bh6aap.top"
echo "  查看日志:  ssh $USER@$HOST 'journalctl -u $SERVICE -f'"
echo "--------------------------------------------------"
