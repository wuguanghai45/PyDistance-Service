"""Read online robot serial numbers from the plant device registry."""

from __future__ import annotations

import json
import re
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener


_ROBOT_SN_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]+$")
# The registry is an internal factory endpoint. Do not inherit HTTP(S)_PROXY
# from the service manager, which can route a private address to an external
# proxy even when command-line curl is configured to bypass it.
_DIRECT_OPENER = build_opener(ProxyHandler({}))


class DeviceRegistryError(RuntimeError):
    """The device registry could not provide a valid device list."""


def fetch_online_robot_sns(url: str, timeout_seconds: float) -> list[str]:
    """Return sorted, unique MQTT-safe serial numbers for online devices.

    The device service is deliberately read by the backend so that browsers do
    not need cross-origin access to the factory-internal registry. The request
    bypasses process proxy settings because the registry is on the plant LAN.
    """
    try:
        request = Request(url, headers={"Accept": "application/json"})
        with _DIRECT_OPENER.open(request, timeout=timeout_seconds) as response:
            payload = json.load(response)
    except (HTTPError, URLError, OSError, TimeoutError, ValueError) as exc:
        raise DeviceRegistryError("无法获取在线机器人列表") from exc

    if not isinstance(payload, list):
        raise DeviceRegistryError("机器人设备服务返回了无效数据")

    return sorted(
        {
            robot_sn
            for device in payload
            if isinstance(device, dict)
            and device.get("is_online") is True
            and isinstance((robot_sn := device.get("robot_sn")), str)
            and _ROBOT_SN_PATTERN.fullmatch(robot_sn)
        }
    )
