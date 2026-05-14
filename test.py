import time
import board
import busio
import adafruit_ads1x15.ads1115 as ADS
from adafruit_ads1x15.analog_in import AnalogIn

# --- 传感器参数配置 (根据说明书规格表 ) ---
D_MIN = 50        # 最小测量距离 50mm
D_MAX = 2500      # CHG-1250 型号最大为 2500mm (若是1400型号请改为4000)
V_MAX = 10.0      # 满量程电压 10V
V_ERROR = 10.1    # 超过此电压视为未检测到目标 (说明书标称无效时10.2V )

# 1. 初始化 I2C
i2c = busio.I2C(board.SCL, board.SDA)

# 2. 初始化 ADS1115
ads = ADS.ADS1115(i2c)

# 3. 设置量程为 6.144V
ads.gain = 2/3

# 4. 初始化通道 A0
chan = AnalogIn(ads, 1)

# 5. 分压补偿系数 (您的电路分压还原系数)
DIVIDER_RATIO = 1.682

print(f"ADS1115 Configured: Monitoring CHG Laser Sensor")
print(f"{'V_Source':>12} | {'Distance (mm)':>15} | {'Status'}")
print("-" * 45)

try:
    while True:
        measured_v = chan.voltage
        real_v = measured_v * DIVIDER_RATIO

        # 距离转换计算
        if real_v > V_ERROR:
            distance_str = "----"
            status = "Out of Range / No Target"
        else:
            # 线性插值公式
            distance = D_MIN + (real_v / V_MAX) * (D_MAX - D_MIN)
            # 边界限幅，防止负数或轻微超标
            distance = max(D_MIN, min(D_MAX, distance))
            distance_str = f"{distance:>.2f}"
            status = "Normal"

        # 打印结果
        print(f"{real_v:>11.4f}V | {distance_str:>13} mm | {status}")

        time.sleep(0.5)

except KeyboardInterrupt:
    print("\n停止监控。")