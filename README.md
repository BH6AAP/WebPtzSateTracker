# YD3040 云台 · 卫星自动跟踪系统

基于雅安 YD3040 云台（Pelco-D 协议）的网页远程控制 + 卫星自动跟踪系统。

## 功能特性

- **网页远程控制**：水平/俯仰手动控制、速度标定、复位、防缠绕复位
- **卫星自动跟踪**：TLE 数据自动更新（6h）、过境列表后台计算（10min）、实时跟踪
- **地图可视化**：卫星星下点轨迹（1h/3h/6h/12h 切换）、覆盖范围圆
- **AS5600 磁编码器反馈**：水平轴绝对角度反馈（UDP 广播），俯仰轴时间估算
- **用户认证**：登录系统，管理员/普通用户角色
- **防缠绕**：累计水平转动量，一键反向回退避免线缆缠绕

## 系统架构

```
┌─────────────┐   UDP 广播(8091)   ┌──────────────────┐
│  ESP32-S3   │ ─────────────────▶ │  j1900 服务器     │
│  AS5600编码器│                     │  Flask + Waitress │
└─────────────┘                     │  (ptz.service)    │
                                    └────────┬─────────┘
                                             │ 串口 (Pelco-D)
                                    ┌────────▼─────────┐
                                    │  YD3040 云台      │
                                    └──────────────────┘
```

- **ESP32-S3**：读取 AS5600 磁编码器，通过 UDP 广播角度数据（局域网内任意设备可接收）
- **j1900 服务器**：Flask 后端（Web 控制 + 卫星跟踪 + 认证），通过串口发送 Pelco-D 指令控制云台
- **前端**：Leaflet 地图 + 控制面板

## 目录结构

```
├── backend/              # Flask 后端
│   ├── main.py           # 主服务 (Web API + 云台控制 + 编码器接收)
│   ├── auth.py           # 用户认证模块
│   ├── satellite.py      # 卫星跟踪 (TLE/过境/位置计算)
│   ├── pelco.py          # Pelco-D 协议
│   └── requirements.txt
├── frontend/             # 前端静态文件
│   ├── index.html        # 控制面板 + 登录
│   └── satellite.js      # 地图 + 卫星逻辑
├── esp32s3_as5600/       # ESP32-S3 编码器固件 (MicroPython)
│   ├── main.py           # AS5600 读取 + UDP 广播
│   └── wifi_manager.py   # WiFi 连接 + AP 配网
├── esp_as5600_tcp/       # ESP32 Arduino 版本 (TCP)
├── docs/                 # 协议文档
│   ├── D3040.md          # YD3040 云台说明
│   └── pelcod.md         # Pelco-D 协议
├── Dockerfile
├── docker-compose.yml
└── deploy.sh             # Linux 一键部署脚本
```

## 快速开始

### 后端 (Linux)

```bash
# 1. 安装依赖
cd backend
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 2. 设置环境变量
export PTZ_SERIAL_PORT=/dev/ttyUSB0
export PTZ_BAUD=9600
export PTZ_ADDRESS=1
export PTZ_PORT=8090
export PTZ_SECRET_KEY=你的随机密钥   # 必填, 用于会话签名

# 3. 启动
python main.py
```

### 一键部署 (已有服务器)

```bash
PTZ_HOST=192.168.31.67 PTZ_USER=aap PTZ_PASS=你的密码 bash deploy.sh
```

### ESP32-S3 固件

1. 将 `esp32s3_as5600/` 下文件上传到 ESP32-S3（MicroPython）
2. 上电后连接 AP 热点 `AS5600-Setup`（密码 `12345678`）
3. 访问 `http://192.168.4.1/` 配置 WiFi
4. ESP32 自动通过 UDP 广播角度数据到端口 8091

## 配置说明

| 环境变量 | 说明 | 默认值 |
|---|---|---|
| `PTZ_SERIAL_PORT` | 串口设备 | `/dev/ttyUSB0` |
| `PTZ_BAUD` | 波特率 | `9600` |
| `PTZ_ADDRESS` | 云台地址 | `1` |
| `PTZ_PORT` | Web 端口 | `8090` |
| `PTZ_SECRET_KEY` | 会话签名密钥（必填） | 无 |

## 协议

- [Pelco-D 协议](docs/pelcod.md)
- [YD3040 云台说明](docs/D3040.md)

## 许可证

MIT License
