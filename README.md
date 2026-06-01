# PyDistance-Service

基于 FastAPI 的轻量级 Web 服务，通过 I2C 总线实时采集 **ADS1115 ADC** 数据，将 **CHG 系列激光位移传感器**（0–10V 输出）的电压转换为物理距离（mm），并通过 RESTful API 对外提供。

- 双通道（A0 / A1）同时采集（可配置）
- 高频后台采样（默认每秒 ~50 次/通道）
- 基于 1 秒滑动窗口的鲁棒滤波（`trimmed_mean` / `median` / `mean`）
- I2C 单例 + 异常容错 + 日志告警
- 内置 Swagger 自动文档（`/docs`）
- Web 仪表盘（`/`）实时显示双通道高度，WebSocket 推送
- 可选 Docker 部署

## 架构

```
CHG Sensor (0-10V) ── 分压电路 ── ADS1115 (A0/A1, 0-6V)
                                       │
                                       │ I2C @0x48
                                       ▼
                          ┌────────────────────────┐
                          │  后台采样线程 (~50Hz)  │
                          │  每通道 deque(ts, V)   │
                          └────────────┬───────────┘
                                       ▼
                          ┌────────────────────────┐
                          │  1 秒窗口滤波           │
                          │  trimmed_mean / median │
                          └────────────┬───────────┘
                                       ▼
                          ┌────────────────────────┐
                          │  FastAPI /api/v1/*     │
                          └────────────────────────┘
```

## 目录结构

```
PyDistance-Service/
├── app/
│   ├── __init__.py
│   ├── main.py          # FastAPI 入口 + lifespan
│   ├── config.py        # pydantic-settings 配置
│   ├── sensor.py        # ADS1115 单例 HAL + 采样线程
│   ├── routes.py        # /api/v1/* + /health
│   ├── ws_routes.py     # WebSocket /ws/distance
│   ├── schemas.py       # Pydantic 响应模型
│   ├── static/          # 高度监测仪表盘（HTML/CSS/JS）
│   └── logger.py        # 日志配置
├── .env.example         # 配置模板
├── requirements.txt
├── scripts/
│   ├── pydistance.service      # systemd 单元模板
│   ├── install-autostart.sh    # 安装开机自启
│   └── uninstall-autostart.sh  # 卸载自启
├── Dockerfile
├── .dockerignore
├── .gitignore
├── test.py              # 原始硬件验证脚本（不依赖 FastAPI）
└── README.md
```

## 硬件准备

### 接线

| ADS1115 | 传感器分压网络 |
|---------|----------------|
| A0      | 传感器 #0 经分压后的电压 |
| A1      | 传感器 #1 经分压后的电压 |
| VDD     | 3.3V / 5V |
| GND     | GND |
| SCL/SDA | 主机 I2C |

> 分压系数 `DIVIDER_RATIO` 默认 `1.682`，需要根据实际分压电阻测量校正。`real_v = adc_v * DIVIDER_RATIO`。

### 启用 I2C（树莓派 / Linux 嵌入式）

```bash
# 树莓派
sudo raspi-config            # Interfacing Options → I2C → Enable

# 通用 Linux：加入 i2c 用户组
sudo usermod -aG i2c $USER

# 检查总线 & 地址（ADS1115 默认 0x48）
sudo apt-get install -y i2c-tools
i2cdetect -y 1
```

## 本地运行

```bash
# 1. 创建虚拟环境
python3 -m venv .venv
source .venv/bin/activate

# 2. 安装依赖
pip install -r requirements.txt

# 3. 配置
cp .env.example .env
# 按实际硬件调整 DIVIDER_RATIO / D_MAX / ADS_CHANNELS ...

# 4. 启动
python -m app.main
# 或：uvicorn app.main:app --host 0.0.0.0 --port 8000
```

打开 <http://localhost:8000/docs> 查看 Swagger UI。

## 开机自启动（systemd）

在树莓派 / 嵌入式 Linux 上，可用安装脚本配置 **本机 venv + systemd** 开机自启（与 `python -m app.main` 一致）：

```bash
chmod +x scripts/install-autostart.sh scripts/uninstall-autostart.sh
sudo ./scripts/install-autostart.sh
```

脚本会：创建/更新 `.venv`、安装依赖、从 `.env.example` 生成 `.env`（若不存在）、将运行用户加入 `i2c` 组、安装并启用 `pydistance.service`。

若 venv 内没有 `pip`（常见于 Orange Pi / 精简 Debian），请先安装系统包再重试：

```bash
sudo apt update
sudo apt install -y python3-venv python3-pip
# 若之前安装失败，可删除残缺 venv 后重装
rm -rf .venv
sudo ./scripts/install-autostart.sh
```

可选环境变量（安装前导出）：

| 变量 | 默认 | 说明 |
|------|------|------|
| `INSTALL_DIR` | 仓库根目录 | 项目路径 |
| `SERVICE_USER` | `sudo` 调用者 | 运行服务的系统用户 |

常用运维：

```bash
sudo systemctl status pydistance
sudo journalctl -u pydistance -f
curl -fsS http://127.0.0.1:8000/health

# 修改 .env 后需重启
sudo systemctl restart pydistance

# 卸载自启（保留 .venv、.env、logs）
sudo ./scripts/uninstall-autostart.sh
```

## Web 仪表盘

启动服务后，在浏览器访问根路径即可查看双通道实时高度：

- **页面**：<http://localhost:8000/>
- **WebSocket**：`ws://localhost:8000/ws/distance`（推送 JSON，结构与 `GET /api/v1/distance` 相同）
- **推送频率**：由 `WS_PUSH_INTERVAL` 控制（默认 `0.1` 秒，约 10 次/秒）

断线后页面会自动重连。

## API

### `GET /api/v1/distance`

返回所有已配置通道的滤波后读数。

响应示例：

```json
{
  "timestamp": "2026-05-14T08:08:22.123Z",
  "channels": [
    {
      "channel": 0,
      "distance_mm": 1250,
      "raw_voltage": 5.023,
      "samples_in_window": 48,
      "status": "Normal",
      "unit": "mm"
    },
    {
      "channel": 1,
      "distance_mm": 980,
      "raw_voltage": 3.945,
      "samples_in_window": 48,
      "status": "Normal",
      "unit": "mm"
    }
  ]
}
```

状态字段语义：
- `Normal` — 测量正常
- `Out of Range` — 电压超过 `V_ERROR`（传感器未检测到目标）
- `Error` — 1 秒窗口内无有效样本（I2C 故障）

### `GET /api/v1/status`

返回硬件健康度。当传感器离线时返回 HTTP 503（响应体仍包含详细信息）。

```json
{
  "sensor_online": true,
  "i2c_address": "0x48",
  "channels": [
    {"channel": 0, "consecutive_failures": 0, "last_status": "Normal", "samples_in_window": 48},
    {"channel": 1, "consecutive_failures": 0, "last_status": "Normal", "samples_in_window": 48}
  ],
  "total_rounds": 12345,
  "actual_sample_rate_hz": 48.5,
  "filter_method": "trimmed_mean",
  "uptime_seconds": 246.7
}
```

### `GET /health`

轻量探针，永远返回 `{"status": "ok"}`。适合 K8s / Docker 健康检查。

### `WebSocket /ws/distance`

持续推送滤波后的距离读数，消息体与 `GET /api/v1/distance` 响应一致。适合仪表盘等需要实时刷新的客户端。

## 配置参数

所有参数通过 `.env` 注入，完整说明见 [.env.example](.env.example)。关键参数：

| 变量 | 默认 | 说明 |
|------|------|------|
| `D_MIN` / `D_MAX` | 50 / 2500 | 测量范围（mm），按传感器型号修改 |
| `V_MAX` / `V_ERROR` | 10.0 / 10.1 | 满量程电压 / 超程阈值 |
| `DIVIDER_RATIO` | 1.682 | 分压补偿系数 |
| `I2C_ADDRESS` | 0x48 | ADS1115 地址 |
| `ADS_CHANNELS` | `[0,1]` | 采集通道列表（JSON 数组） |
| `ADS_DATA_RATE` | 250 | ADS1115 SPS：8/16/32/64/128/250/475/860 |
| `SAMPLE_INTERVAL` | 0.02 | 一轮采样周期（秒） |
| `WINDOW_SECONDS` | 1.0 | 滤波窗口长度（秒） |
| `FILTER_METHOD` | `trimmed_mean` | `mean` / `median` / `trimmed_mean` |
| `TRIM_RATIO` | 0.2 | 截尾均值两端裁剪比例 |
| `ANOMALY_JUMP_MM` | 50.0 | 距离跳变阈值（mm），超过即写 WARNING |
| `WS_PUSH_INTERVAL` | 0.1 | WebSocket 推送间隔（秒） |

## Docker 部署

构建：

```bash
docker build -t pydistance-service .
```

运行（需将 I2C 设备挂入容器）：

```bash
docker run -d \
  --name pydistance \
  --device /dev/i2c-1 \
  -p 8000:8000 \
  -v $(pwd)/.env:/app/.env:ro \
  -v $(pwd)/logs:/app/logs \
  pydistance-service
```

若容器内无法直接访问 I2C，可改用 `--privileged`。

健康检查：

```bash
curl http://localhost:8000/health
curl http://localhost:8000/api/v1/distance | jq
```

## 故障排查

| 现象 | 排查 |
|------|------|
| `/api/v1/status` 返回 503，`sensor_online=false` | 检查 I2C 是否启用 (`i2cdetect -y 1`)；ADS1115 是否上电；`I2C_ADDRESS` 是否正确 |
| `actual_sample_rate_hz` 远低于预期 | 调大 `ADS_DATA_RATE`，减小 `SAMPLE_INTERVAL`；检查 I2C 总线是否被其他设备占用 |
| `distance_mm` 抖动大 | 增大 `WINDOW_SECONDS`、使用 `median`；或检查分压电路稳定性 |
| `status="Out of Range"` 持续出现 | 实测电压超过 `V_ERROR`；确认传感器有目标 & 分压系数正确 |
| 日志频繁出现距离跳变 WARNING | 根据应用场景调大 `ANOMALY_JUMP_MM`，或排查激光闪烁/电源干扰 |

## 风险与注意事项

- **I2C 地址冲突**：ADS1115 默认 `0x48`；若与其他设备冲突，将 ADDR 引脚接 VDD/SDA/SCL 切换为 `0x49/0x4A/0x4B`，并修改 `.env`
- **单 ADC 核心**：ADS1115 内部仅一个 ADC，多通道是 MUX 切换 + 串行转换，每增加一个通道会同等增加单轮耗时
- **平台兼容**：硬件库 `adafruit-blinka` 仅在 Linux/嵌入式正常工作，开发机 (macOS/Windows) 上服务可以启动但 `sensor_online=false`
- **`.env` 修改后需重启**：所有配置在进程启动时加载一次，未实现热加载
