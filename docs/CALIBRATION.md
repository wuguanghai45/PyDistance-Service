# 蚂蚁机器人高度标定

## 范围

在现有 FastAPI 服务中执行八项高度测量、记录和公差判定。不会将测量值写回机器人固件，也不会自动修改机器人补偿参数。

- A/B：举升平台的前后测量位置，与 ADC 通道编号无关。
- H/L：高位/低位，指令高度在工位配置中指定。
- Y/N：负载/空载。
- 标定高度：`本任务地面基准距离 - 当前激光距离`，单位 mm。
- 一次任务使用同一个测量通道和固定地面基准；不能中途更换基准。

## 本地模拟

安装 `requirements.txt` 后，按原方式启动 `python -m app.main`，访问 `/calibration.html`。

1. 点击“载入演示配置”。演示坐标并非生产工位坐标。
2. 点击“保存新版本”。配置按不可变版本保存，修改不会影响正在运行的任务。
3. 输入 robotLabel，或使用演示填入的 `DEMO-ANT`，选择“模拟”。
4. 点击“开始标定”，查看全部八点、归还料箱和到达完成点的过程。
5. 导出 CSV 汇总或 JSON 完整任务/命令/测量记录。

模拟不创建 MQTT 客户端，不读取真实激光数据；使用 2000 mm 合成地面距离，缩短动作和稳定等待。模式写入数据库及导出，不能用于实际验收。真实传感器面板独立显示，非硬件开发机上显示 Error 不影响模拟。

服务只允许一个进程：不要使用多个 Uvicorn workers。数据库文件锁阻止第二个服务同时接管该工位。不同数据库对应不同锁；生产环境不得用多个数据库/副本控制同一工位。

## 实机启用前必须完成

### 1. 设置服务器连接与权限

在 `.env` 中配置（示意，不含实际凭据）：

```dotenv
CALIBRATION_LIVE_ENABLED=true
CALIBRATION_API_TOKEN=replace-with-a-long-random-operator-token
MQTT_HOST=your-broker-host
MQTT_PORT=8883
MQTT_USERNAME=your-user
MQTT_PASSWORD=your-password
MQTT_TLS=true
ROBOT_SN_MAP={"YOUR-SN":"YOUR-ROBOT-LABEL"}
```

TLS 默认验证服务器证书及主机名，可通过 `MQTT_CA_FILE` 指定工厂 CA。若现场明确使用非 TLS 的隔离网络 broker，则配置相应端口与 `MQTT_TLS=false`。不会从参考项目复制凭据。

设置操作令牌后，所有标定 HTTP API 都要求 `Authorization: Bearer <token>`。在网页输入令牌后点击“连接服务”。令牌只保留在本页，不写入浏览器存储或 WebSocket URL。WebSocket 在连接后的第一帧提交令牌，5 秒内未认证会断开。

网页实机选项只有在 LIVE_ENABLED、令牌和 broker host 均已配置时才启用。部署到网络时应使用 HTTPS/可信反向代理、防火墙和受控工厂网络，避免令牌明文传输。原 `/distance` 和 `/ws/distance` 仍保持原有只读行为。

SN 只通过服务器 `ROBOT_SN_MAP` 映射；未知 SN 返回错误，不推断其 Label。也可直接选择 robotLabel 输入。

### 2. 确认现场测量条件

- 机器人初始空载、静止，位于配置的起始扫码点及朝向；起始点与标定点不能重合。
- 点击开始时光路必须照到地面，机器人、料箱等不能遮挡。服务在下发任何 MQTT 命令前记录地面基准。
- 激光必须照射同一个平台测量面。带箱后若光束打到料箱而非平台，公式测得的是料箱表面高度，不能作为平台高度。
- **负载低位仍必须承重。** 如果 L 位时料箱落到地面/支架，BLY/ALY 就不是负载测量，需要先改造工装或调整低位工艺。
- 空载高位旋转、负载低位旋转、携箱移动均须经过现场安全验证；原地旋转 180° 后传感器应落在另一端测量点。
- 标定控制器运行时，其他调度系统不得向同一机器人下发指令。软件工位锁不能约束其他独立系统。
- 现场硬件急停必须可用。网页“取消”不是急停。

上述关键条件在每次实机启动前需要勾选确认。

### 3. 配置点位、朝向与路线

点位结构为 `{"code":"地码内容","x":1000,"y":0,"orientation":0}`。坐标 mm，网页角度为度，发送 MQTT 时转成 0.01°。

| 点位 | 含义 |
|---|---|
| start | 初始地码及机器人真实初始朝向 |
| calibration | 标定位置，orientation 表示激光对准 A 点的机器人朝向 |
| bin | 取箱位置，orientation 必须为 A 朝向 + 180°，即 B 测量朝向 |
| storage | 可选独立存放位置；省略时归还到 bin 的坐标/码值，归还时保持 A 朝向 |
| finish | 结束地码及最终朝向 |

取箱/返回/归还三段使用固定朝向直线移动：料箱点和存放点必须在标定点 A 朝向的正前方同一直线上。后端提前校验，不会自行规划绕障路线。

例如 A 朝向为 0°（+X）：标定点在 X=1000，料箱点在 X=2000。空载旋转到 B 朝向 180°后，倒车到 X=2000；取箱后保持 180°朝向，前进返回 X=1000。两次都倒车无法在这条直线路线上返回原点。最后旋回 A 朝向后，前进到 X=2000 归还。

“路线、反馈与高级参数”支持：

- `approach_waypoints`：起始点到标定点之间按顺序经过的地码列表。
- `exit_waypoints`：放箱后到完成点之间按顺序经过的地码列表。
- 每个路段先对准目标方位，再直线前进，抵达后转到该点配置朝向。
- `obstacle_avoidance`：默认 true。参考脚本为 false，本服务不会默认沿用关闭避障的设置；需确认固件支持与工位实际行为。

### 4. 配置工艺与反馈

`low_height_mm` / `high_height_mm` 是举升行程指令，**不是**激光测得的平台离地高度。应根据车型填写，不能直接把期望平台高度填到举升指令中。

默认稳定时间为 2 秒，下限也是 2 秒。之后额外采集默认 0.5 秒的新样本窗口；因此实际记录发生在动作确认完成约 2.5 秒之后。

关键高级参数：

| 参数 | 默认 | 说明 |
|---|---|---|
| sample_window_seconds | 0.5 | 稳定后采样时间 |
| min_samples | 5 | 最少有效样本数，按实际采样率调整 |
| max_sample_age_seconds | 0.2 | 最新样本最大年龄 |
| max_spread_mm | 3 | 距离窗口极差上限，不稳定则失败 |
| command_timeout_seconds | 60 | 连接/命令完成/到位等待上限 |
| confirmation_timeout_seconds | 300 | 取放箱人工或载荷反馈确认超时 |
| telemetry_timeout_seconds | 3 | 机器人状态超过此时长视为离线 |
| position_tolerance_mm | 10 | 坐标到位公差 |
| orientation_tolerance_deg | 2 | 朝向到位公差 |
| lift_tolerance_mm | 3 | 举升到位公差 |
| velocity / acceleration | 100 / 100 | 低速调试初始参数，最高配置限制 500 |
| allow_set_origin | false | 是否允许按已确认的 start 设置原点 |
| scan_valid_value | true | 固件 qrCodeStatus 的有效值；按真实报文配置 |
| scan_code_field | null | 可选扫码内容字段，例如 `scannerStatus.scannerData`；配置后核对地码内容 |
| load_feedback_field | null | 可选载荷字段路径；必须返回 bool 或 0/1 |

参考项目 Ant 状态只明确读取了 `qrCodeStatus`、坐标、朝向、举升高度，没有确认载荷字段。默认按扫码状态 + 位置 + 朝向核验到位；只有配置 `scan_code_field` 后才核验实际码字符串，不宣称已经验证未提供的字段。

`load_feedback_field` 未配置时，取箱和放箱分别停在网页确认步骤。配置后采用反馈自动继续，并要求空载测量为 false、负载测量为 true；反馈缺失不能自动当作成功。人工确认模式的 Y 测量依赖现场工装确保低位始终承重，并记录为 `operator_confirmed`，不伪造自动载荷证据。

初始化根据状态执行：UNKNOWN → INIT；若结果为 LOCATION_UNKNOWN，只有 `allow_set_origin=true` 才发送 HOME_SET_ORIGIN。最终必须 IDLE 且起点位姿匹配；已处于 IDLE 的机器人不重复强制初始化。

## 固定执行顺序

| 序号 | 动作/位置 | 测量 |
|---:|---|---|
| 1 | 启动时地面无遮挡 | 保存本次地面基准 |
| 2 | 初始化/必要时设置原点，确认起点，下降至低位 | — |
| 3 | 按接近路线到标定点，朝向 A，低位稳定 | ALN |
| 4 | A 面升至 H，稳定 | AHN |
| 5 | 高位旋转 180°至 B，稳定 | BHN |
| 6 | B 面降至 L，稳定 | BLN |
| 7 | 保持 B 朝向倒车至料箱点，升到 H，确认取箱 | — |
| 8 | 保持 B 朝向前进返回标定点，高位稳定 | BHY |
| 9 | B 面降至 L，稳定 | BLY |
| 10 | 低位旋转 180°至 A，稳定 | ALY |
| 11 | A 面升至 H，稳定 | AHY |
| 12 | 高位正向移动至存放点，降至 L，确认归还 | — |
| 13 | 按离开路线到完成点，确认最终位姿 | 流程完成 |

命令一次只发一个，每个 UUID 独立关联结果。动作以匹配的 COMPLETE_SUCCESS 和其后新收到的静止/到位状态共同确认，旧 IDLE、其他命令结果、保留状态消息不能完成当前动作。重复完成消息不导致重复下发。断线不重连重发当前任务。

测量保留浮点精度和原始电压样本；原距离展示 API 仍按整毫米显示。任何超量程样本、近期采集失败、样本过期、样本不足或测量极差过大都会中止，绝不采用前一个测量值补齐。

## 结果与安全恢复

数据位于 `CALIBRATION_DB`，默认 `data/calibration.sqlite3`，包含配置快照、基准、八项结果、机器人状态、命令内容/结果和操作员确认事件。凭据不写入任务快照。

任务 `COMPLETED` 表示八项齐全、料箱已归还且已到完成点；与验收 `PASS` 是不同概念。高度不合格时保存 `FAIL`，正常归还并离开；未配置全部公差则总体 `NOT_EVALUATED`，不会判合格。

失效或取消：停止后续调度，保留数据，禁止自动归位/自动下降/自动续跑。当前已接受的物理动作可能继续执行。任何实机 FAILED、CANCELLED 或服务重启后的 INTERRUPTED 都会保持工位锁，操作员应使用现场控制确保停止并确认料箱安全，然后点“确认安全并解锁”。默认没有自动参数写回和重试恢复。

硬件急停命令未在参考协议中得到确认，不能将 MQTT 断开、取消任务或某个猜测的 STOP 命令当作硬件急停。实机联调后才能按确定的停机协议扩展。

SQLite 目录需可写。Docker 部署应把 `/app/data` 作为持久卷；不要因更新容器而丢失记录和未解除的工位锁。建议停服务后备份整个 data 目录，或使用 SQLite 在线备份工具，不要只复制处于写入状态的主数据库文件而忽略 WAL。

## 接口

所有路径以下列前缀开始：`/api/v1/calibration`。

| 方法/路径 | 用途 |
|---|---|
| GET /system | 实机可用性、工位占用和演示模板，不返回 broker 凭据 |
| GET /configs | 配置版本列表 |
| POST /configs | 保存新配置版本 |
| POST /tasks | 启动，mode 默认 simulation |
| GET /tasks?limit=50 | 历史摘要，最多 200 条 |
| GET /tasks/{id} | 完整任务快照与实时状态 |
| GET /tasks/{id}/result | 完整或部分测量值 |
| GET /tasks/{id}/events?after=0 | 顺序事件，500 条一页，以最后 seq 继续读取 |
| POST /tasks/{id}/cancel | 停止后续调度，不是硬件急停 |
| POST /tasks/{id}/confirm | `{"step":"CONFIRM_PICKUP","confirmed":true}`，放箱步骤为 CONFIRM_DROP |
| POST /tasks/{id}/release | `{"robot_stopped_and_station_safe":true}`，仅终态可解锁 |
| GET /tasks/{id}/export | 八项 CSV，未测项为 MISSING，保留模式/任务状态 |

WebSocket：`/ws/calibration/{id}`，第一帧 `{"token":"操作令牌"}`，无令牌部署时仍发送 `{"token":""}`。推送任务快照和新增事件；任务终态后关闭，网页自动恢复活动任务连接。

## 自动化验证与实机验收

```bash
python -m pip install -r requirements.txt -r requirements-dev.txt
python -m pytest -q
```

测试全部使用内存机器人/假 MQTT 传输/假传感器，不访问实际 broker 或 I2C。覆盖顺序、位置/朝向、公差、SN 映射、鉴权、CSV、WebSocket、人工确认、并发工位锁、取消、异常、重启、命令相关性及传感器新鲜度。

正式投入前仍需现场验收：先核对固件报文和单个低速动作，再空载四项，再验证取放箱及低位承重，最后完成八项闭环；测试断网、激光遮挡、取消及现场急停后的人工恢复。模拟测试不能验证机械安全、真实测量精度或固件协议兼容性。
