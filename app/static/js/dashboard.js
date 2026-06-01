(function () {
  "use strict";

  const WS_PATH = "/ws/distance";
  const RECONNECT_BASE_MS = 1000;
  const RECONNECT_MAX_MS = 5000;

  const CHANNEL_DOM = {
    0: {
      card: document.getElementById("card-ch0"),
      value: document.getElementById("value-ch0"),
      status: document.getElementById("status-ch0"),
      voltage: document.getElementById("voltage-ch0"),
      samples: document.getElementById("samples-ch0"),
    },
    1: {
      card: document.getElementById("card-ch1"),
      value: document.getElementById("value-ch1"),
      status: document.getElementById("status-ch1"),
      voltage: document.getElementById("voltage-ch1"),
      samples: document.getElementById("samples-ch1"),
    },
  };

  const connectionEl = document.getElementById("connection");
  const connectionLabel = document.getElementById("connection-label");
  const timestampEl = document.getElementById("timestamp");

  const lastValues = { 0: null, 1: null };
  let ws = null;
  let reconnectDelay = RECONNECT_BASE_MS;
  let reconnectTimer = null;
  let intentionalClose = false;

  function wsUrl() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    return proto + "//" + location.host + WS_PATH;
  }

  function setConnection(online, label) {
    connectionEl.classList.toggle("connection--online", online);
    connectionEl.classList.toggle("connection--offline", !online);
    connectionLabel.textContent = label;
  }

  function statusClass(status) {
    switch (status) {
      case "Normal":
        return "badge--normal";
      case "Out of Range":
        return "badge--warning";
      case "Error":
        return "badge--error";
      default:
        return "badge--unknown";
    }
  }

  function formatDistance(mm) {
    if (mm === null || mm === undefined) {
      return null;
    }
    return String(Math.trunc(Number(mm)));
  }

  function updateChannel(reading) {
    const ch = reading.channel;
    const dom = CHANNEL_DOM[ch];
    if (!dom) {
      return;
    }

    const formatted = formatDistance(reading.distance_mm);
    const display = formatted !== null ? formatted : "—";

    if (formatted !== null && formatted !== lastValues[ch]) {
      dom.value.classList.add("card__value--flash");
      dom.card.classList.add("card--pulse");
      setTimeout(function () {
        dom.value.classList.remove("card__value--flash");
        dom.card.classList.remove("card--pulse");
      }, 450);
      lastValues[ch] = formatted;
    }

    dom.value.textContent = display;
    dom.value.classList.toggle("card__value--null", formatted === null);

    dom.status.textContent = reading.status || "—";
    dom.status.className = "badge " + statusClass(reading.status);

    dom.voltage.textContent =
      reading.raw_voltage !== undefined
        ? reading.raw_voltage.toFixed(4) + " V"
        : "—";
    dom.samples.textContent =
      reading.samples_in_window !== undefined
        ? String(reading.samples_in_window)
        : "—";
  }

  function handleMessage(event) {
    let data;
    try {
      data = JSON.parse(event.data);
    } catch (e) {
      console.warn("Invalid JSON from WebSocket", e);
      return;
    }

    if (data.timestamp) {
      const d = new Date(data.timestamp);
      timestampEl.textContent = isNaN(d.getTime())
        ? data.timestamp
        : d.toLocaleString("zh-CN", { hour12: false });
    }

    if (Array.isArray(data.channels)) {
      data.channels.forEach(updateChannel);
    }
  }

  function scheduleReconnect() {
    if (intentionalClose || reconnectTimer !== null) {
      return;
    }
    setConnection(false, "重连中… (" + reconnectDelay / 1000 + "s)");
    reconnectTimer = setTimeout(function () {
      reconnectTimer = null;
      connect();
    }, reconnectDelay);
    reconnectDelay = Math.min(reconnectDelay * 2, RECONNECT_MAX_MS);
  }

  function connect() {
    intentionalClose = false;
    setConnection(false, "连接中…");

    ws = new WebSocket(wsUrl());

    ws.onopen = function () {
      reconnectDelay = RECONNECT_BASE_MS;
      setConnection(true, "已连接");
    };

    ws.onmessage = handleMessage;

    ws.onclose = function () {
      setConnection(false, "已断开");
      ws = null;
      scheduleReconnect();
    };

    ws.onerror = function () {
      setConnection(false, "连接错误");
    };
  }

  window.addEventListener("beforeunload", function () {
    intentionalClose = true;
    if (reconnectTimer !== null) {
      clearTimeout(reconnectTimer);
    }
    if (ws) {
      ws.close();
    }
  });

  connect();
})();
