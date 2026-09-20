#!/usr/bin/env bash
# ============================================================
# YD3040 云台 · 卫星跟踪系统 —— Linux 一键部署脚本
# 功能: 同步后端/前端代码到服务器 -> 安装依赖 -> 重启服务 -> 验证
# 用法: bash deploy.sh
# ============================================================
set -euo pipefail

# ---------- 服务器配置 (全部通过环境变量注入, 脚本内不硬编码任何个人信息) ----------
# 用法:
#   PTZ_HOST=<服务器IP> PTZ_USER=<SSH用户> PTZ_PASS=<密码> \
#   PTZ_REMOTE_DIR=/home/<用户>/ptz PTZ_WEB_PORT=8090 PTZ_UDP_PORT=8091 \
#   PTZ_PUBLIC_URL=https://<外网域名> bash deploy.sh
HOST="${PTZ_HOST:-}"
USER="${PTZ_USER:-}"
PASS="${PTZ_PASS:-}"
REMOTE_DIR="${PTZ_REMOTE_DIR:-}"
SERVICE="${PTZ_SERVICE:-ptz}"
WEB_PORT="${PTZ_WEB_PORT:-8090}"
UDP_PORT="${PTZ_UDP_PORT:-8091}"
PUBLIC_URL="${PTZ_PUBLIC_URL:-}"

# 缺失必填项则提示输入 (IP/用户/远程路径不能有默认值, 避免误部署到他人服务器)
if [ -z "$HOST" ]; then read -rp "请输入服务器 IP: " HOST; fi
if [ -z "$USER" ]; then read -rp "请输入 SSH 用户名: " USER; fi
if [ -z "$REMOTE_DIR" ]; then read -rp "请输入远程代码目录 (如 /home/xxx/ptz): " REMOTE_DIR; fi
if [ -z "$PASS" ]; then read -rsp "请输入服务器 SSH 密码: " PASS; echo; fi

# 校验必填项
[ -n "$HOST" ] && [ -n "$USER" ] && [ -n "$REMOTE_DIR" ] || fail "缺少服务器配置 (IP/用户/远程目录)"

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
HTTP_CODE=$($SSH "curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:$WEB_PORT/api/auth/me" 2>/dev/null || echo "000")
info "HTTP 接口 /api/auth/me 返回: $HTTP_CODE (401=正常, 需登录)"

# 验证 UDP 编码器监听
UDP_OK=$($SSH "ss -ulnp | grep -q $UDP_PORT && echo yes || echo no" 2>/dev/null || echo "no")
if [ "$UDP_OK" = "yes" ]; then
    info "UDP 编码器监听 $UDP_PORT: 正常 ✓"
else
    warn "UDP 编码器监听 $UDP_PORT: 未检测到"
fi

info "部署完成!"
echo "--------------------------------------------------"
echo "  本地访问:  http://$HOST:$WEB_PORT"
if [ -n "$PUBLIC_URL" ]; then echo "  外网访问:  $PUBLIC_URL"; fi
echo "  查看日志:  ssh $USER@$HOST 'journalctl -u $SERVICE -f'"
echo "--------------------------------------------------"
