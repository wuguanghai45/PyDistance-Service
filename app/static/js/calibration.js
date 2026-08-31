/* Station configuration and task controls; all external text is rendered with textContent. */
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const sensorChannel = 1; // ADS1115 A1: use the hardware index, not a display ordinal.
  const channelLabel = (channel) => Number.isInteger(channel) && channel >= 0 && channel <= 3 ? `ADS1115 A${channel}（channel=${channel}）` : "传感器通道未记录";
  const order = ["ALN", "AHN", "BHN", "BLN", "BHY", "BLY", "ALY", "AHY"];
  const points = { start: "起始点", calibration: "标定点 A", bin: "取箱点 B", storage: "存放点", finish: "完成点" };
  const process = {
    low_height_mm: ["低位指令 / mm", 0, 1000, 1],
    high_height_mm: ["高位指令 / mm", 1, 1000, 1], settle_seconds: ["稳定等待 / s", 2, 30, 0.1],
    command_timeout_seconds: ["命令超时 / s", 1, 600, 1], velocity: ["移动速度 / mm/s", 1, 1000, 1],
    acceleration: ["移动加速度 / mm/s²", 1, 500, 1],
  };
  const labels = { RUNNING: "执行中", CANCELLING: "取消中", COMPLETED: "流程完成", FAILED: "异常停止", CANCELLED: "已取消", INTERRUPTED: "重启中断", PASS: "合格", FAIL: "不合格", NOT_EVALUATED: "未配置标准", PENDING: "待采集" };
  let recipes = [], currentConfig = null, configId = null, currentTask = null, dirty = true, configFormTouched = false;
  let system = null, stationOwner = null, taskSocket = null, sensorSocket = null, reconnect = null, stopped = false;
  let currentView = null, generation = 0, seenEvents = new Set();
  let robotRecords = [], robotPickerOpen = false, robotPickerActive = -1;

  function el(tag, text, attrs = {}) {
    const node = document.createElement(tag);
    if (text !== null) node.textContent = text;
    Object.entries(attrs).forEach(([key, value]) => node.setAttribute(key, value));
    return node;
  }

  function message(text, error = false) { $("message").textContent = text; $("message").className = error ? "error" : ""; }
  async function api(path, body, method = body === undefined ? "GET" : "POST") {
    const headers = { "Content-Type": "application/json" };
    const response = await fetch(`/api/v1/calibration${path}`, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
    if (!response.ok) {
      const detail = await response.json().catch(() => ({ detail: response.statusText }));
      throw new Error(typeof detail.detail === "string" ? detail.detail : JSON.stringify(detail.detail));
    }
    return response;
  }
  async function json(path, body) { return (await api(path, body)).json(); }
  function handle(action) { return async (event) => { event?.preventDefault(); try { await action(event); } catch (error) { message(error.message, true); } }; }
  function updateStart() {
    $("start").disabled = !system?.live_enabled || !configId || dirty || !!stationOwner || !$("identity").value;
  }
  function setRecipeState(text) {
    $("recipe-state").textContent = text;
    $("config-summary-state").textContent = text;
  }
  function markDirty() {
    dirty = true;
    configFormTouched = true;
    setRecipeState("有未保存更改，请先保存新版本");
    updateStart();
  }

  for (const [key, title] of Object.entries(points)) {
    const row = el("tr", null, { id: `row-${key}` }); row.append(el("td", title));
    for (const field of ["code", "x", "y", "orientation"]) {
      const cell = el("td", null), input = el("input", null, { id: `${key}-${field}`, "aria-label": `${title} ${field}`, type: field === "code" ? "text" : "number", required: "" });
      if (field === "orientation") { input.min = 0; input.max = 359.99; input.step = 0.01; }
      cell.append(input); row.append(cell);
    }
    $("point-fields").append(row);
  }
  for (const [key, [title, min, max, step]] of Object.entries(process)) {
    const label = el("label", title), input = el("input", null, { id: key, type: "number", min, max, step, required: "" });
    label.append(input); $("process-fields").append(label);
  }
  for (const key of order) {
    const row = el("tr", null);
    const check = el("input", null, { type: "checkbox", id: `limit-${key}`, "aria-label": `启用 ${key} 验收` });
    const cell = el("td", null); cell.append(check); row.append(cell, el("td", key));
    for (const field of ["target", "tolerance"]) {
      const td = el("td", null), input = el("input", null, { type: "number", step: "0.01", id: `${field}-${key}`, "aria-label": `${key} ${field}` });
      if (field === "tolerance") input.min = 0;
      td.append(input); row.append(td);
    }
    $("limit-fields").append(row);
  }

  function storageVisibility() {
    $("row-storage").hidden = !$("separate-storage").checked;
    $("row-storage").querySelectorAll("input").forEach((input) => { input.disabled = !$("separate-storage").checked; });
  }
  function loadRecipe(recipe, id = null) {
    currentConfig = structuredClone(recipe); configId = id;
    configFormTouched = false;
    $("active-config-name").textContent = recipe.name || "未命名工位配置";
    $("config-name").value = recipe.name;
    for (const key of Object.keys(points)) {
      const point = recipe[key] || recipe.bin;
      for (const field of ["code", "x", "y", "orientation"]) $(`${key}-${field}`).value = point[field];
      if (key === "storage" && !recipe.storage) $("storage-orientation").value = recipe.calibration.orientation;
    }
    $("separate-storage").checked = !!recipe.storage; storageVisibility();
    Object.keys(process).forEach((key) => { $(key).value = recipe[key]; });
    const advanced = structuredClone(recipe);
    for (const key of ["name", "limits", "sensor_channel", ...Object.keys(points), ...Object.keys(process)]) delete advanced[key];
    $("advanced").value = JSON.stringify(advanced, null, 2);
    for (const key of order) {
      const limit = recipe.limits[key];
      $(`limit-${key}`).checked = !!limit;
      $(`target-${key}`).value = limit?.target_mm ?? "";
      $(`tolerance-${key}`).value = limit?.tolerance_mm ?? "";
    }
    const unsupportedChannel = recipe.sensor_channel !== sensorChannel;
    dirty = !id || unsupportedChannel;
    setRecipeState(unsupportedChannel ? `旧配置：${channelLabel(recipe.sensor_channel)}。请保存为 A1（channel=1）的新版本后启动` : (id ? `已保存版本 ${id.slice(0, 8)}` : "演示参数尚未保存"));
    $("config-select").value = id || ""; updateStart();
  }
  function readRecipe() {
    const value = JSON.parse($("advanced").value);
    if (!value || Array.isArray(value) || typeof value !== "object") throw new Error("高级参数必须为 JSON 对象");
    value.name = $("config-name").value.trim();
    value.sensor_channel = sensorChannel;
    for (const key of Object.keys(points)) {
      value[key] = { code: $(`${key}-code`).value.trim(), x: Number($(`${key}-x`).value), y: Number($(`${key}-y`).value), orientation: Number($(`${key}-orientation`).value) };
    }
    if (!$("separate-storage").checked) value.storage = null;
    Object.keys(process).forEach((key) => { value[key] = Number($(key).value); });
    value.limits = {};
    for (const key of order) if ($(`limit-${key}`).checked) {
      if (!$(`target-${key}`).value || !$(`tolerance-${key}`).value) throw new Error(`${key} 的目标和公差不能为空`);
      value.limits[key] = { target_mm: Number($(`target-${key}`).value), tolerance_mm: Number($(`tolerance-${key}`).value) };
    }
    return value;
  }
  async function refreshRecipes() {
    recipes = await json("/configs"); $("config-select").replaceChildren(el("option", "选择工位配置", { value: "" }));
    recipes.forEach((record) => $("config-select").append(el("option", `${record.config.name} · ${record.id.slice(0,8)}`, { value: record.id })));
    $("config-select").value = configId || "";
  }
  function closeConfigDialog() {
    const dialog = $("config-dialog");
    if (configFormTouched && !window.confirm("当前工位配置有未保存更改。确定关闭窗口并放弃这些更改吗？")) return;
    dialog.close();
  }
  $("open-config").addEventListener("click", () => {
    const dialog = $("config-dialog");
    dialog.showModal();
    $("close-config").focus();
  });
  $("close-config").addEventListener("click", closeConfigDialog);
  $("config-dialog").addEventListener("cancel", (event) => {
    event.preventDefault();
    closeConfigDialog();
  });
  $("config-dialog").addEventListener("click", (event) => {
    if (event.target === event.currentTarget) closeConfigDialog();
  });
  $("config-dialog").addEventListener("close", () => $("open-config").focus());
  $("config-form").addEventListener("input", markDirty);
  $("separate-storage").addEventListener("change", storageVisibility);
  $("config-select").addEventListener("change", () => {
    const selectedId = $("config-select").value;
    if (configFormTouched && !window.confirm("当前工位配置有未保存更改。确定放弃这些更改并切换版本吗？")) {
      $("config-select").value = configId || "";
      return;
    }
    const record = recipes.find((item) => item.id === selectedId);
    if (record) loadRecipe(record.config, record.id);
  });
  $("config-form").addEventListener("submit", handle(async () => {
    const record = await json("/configs", readRecipe()); await refreshRecipes(); loadRecipe(record.config, record.id); message("工位配置已保存为新版本。");
  }));
  $("delete-config").addEventListener("click", handle(async () => {
    if (!configId || !currentConfig) {
      throw new Error("请先从“已保存版本”中选择要删除的配置");
    }
    const name = currentConfig.name || "当前配置";
    if (!window.confirm(`确定删除配置“${name}”吗？已启动和历史标定任务不受影响。`)) return;
    await api(`/configs/${encodeURIComponent(configId)}`, undefined, "DELETE");
    configId = null;
    dirty = true;
    configFormTouched = false;
    await refreshRecipes();
    $("active-config-name").textContent = "当前配置已删除";
    setRecipeState("配置已删除；当前参数尚未保存");
    updateStart();
    message(`配置“${name}”已删除。`);
  }));
  $("start-form").addEventListener("submit", handle(async () => {
    if (dirty || !configId) throw new Error("请先保存工位配置");
    $("start").disabled = true;
    try {
      const task = await json("/tasks", { config_id: configId,
        identity: $("identity").value, mode: "live",
        ground_clear_confirmed: $("ground-clear").checked, robot_at_start_confirmed: $("robot-at-start").checked,
        route_safe_confirmed: $("route-safe").checked, loaded_low_safe_confirmed: $("loaded-low-safe").checked,
        live_motion_confirmed: $("live-motion").checked });
      stationOwner = task.id; await viewTask(task.id); message("实机标定已启动，请留意现场安全。");
    } finally { updateStart(); }
  }));

  const number = (value) => value === null || value === undefined ? "—" : Number(value).toFixed(3);
  const blockerLabels = {
    mainState: "主状态非 IDLE",
    velocity: "线速度非零",
  };
  function velocityIsZero(state) {
    if (!("velocity" in state)) return true;
    const value = Number(state.velocity);
    return Number.isFinite(value) && Math.abs(value) < 1e-3;
  }
  function diagnosticsFromState(state) {
    const blockers = [];
    if (state.mainState !== "IDLE") blockers.push({ field: "mainState", value: state.mainState, reason: blockerLabels.mainState });
    else if (!velocityIsZero(state)) blockers.push({ field: "velocity", value: state.velocity, reason: blockerLabels.velocity });
    return { stationary: state.mainState === "IDLE" && velocityIsZero(state), blockers, telemetry_age_s: null, mqtt_connected: null };
  }
  function motionText(state, key) {
    if (!(key in state)) return "未上报";
    const value = Number(state[key]);
    if (!Number.isFinite(value)) return String(state[key]);
    return `${number(value)} mm/s${Math.abs(value) < 1e-3 ? "（零）" : "（非零）"}`;
  }
  function renderRobotDiagnostics(task) {
    const state = task.robot_state || {};
    const diag = task.robot_diagnostics || diagnosticsFromState(state);
    const hasState = !!state.mainState;
    $("robot-main-state").textContent = hasState ? `机器人 ${state.mainState}` : "等待机器人状态";
    $("robot-stationary-pill").hidden = !hasState;
    if (hasState) {
      const pill = $("robot-stationary-pill");
      pill.textContent = diag.stationary ? "满足采样静止条件" : "未满足采样静止条件";
      pill.className = diag.stationary ? "pill ok" : "pill warn";
    }
    $("robot-pose").hidden = !hasState;
    $("robot-motion").hidden = !hasState;
    if (hasState) {
      $("robot-pose").textContent = `位姿 · X ${number(state.coordX)} / Y ${number(state.coordY)} · 朝向 ${number(state.orientation / 100)}° · 举升 ${number(state.liftHeight)} mm`;
      $("robot-motion").textContent = `运动 · 线速度 ${motionText(state, "velocity")} · 角速度 ${motionText(state, "angularVelocity")}`;
      $("robot-motion").className = diag.stationary ? "hint" : "hint warn-text";
    }
    const extras = [];
    if (state.qrCodeStatus !== undefined && state.qrCodeStatus !== null) extras.push(`扫码 ${state.qrCodeStatus}`);
    if (diag.telemetry_age_s != null) extras.push(`状态 ${diag.telemetry_age_s.toFixed(1)} s 前更新`);
    if (diag.mqtt_connected != null) extras.push(diag.mqtt_connected ? "MQTT 已连接" : "MQTT 未连接");
    if (diag.blockers?.length) {
      extras.push(`阻塞：${diag.blockers.map((item) => `${blockerLabels[item.field] || item.reason}${item.value == null ? "" : ` (${item.value})`}`).join("；")}`);
    }
    $("robot-extra").hidden = !extras.length;
    $("robot-extra").textContent = extras.join(" · ");
  }
  function renderTask(task) {
    currentTask = task;
    $("task-title").textContent = task.step_title;
    $("task-status").textContent = labels[task.status] || task.status;
    $("task-meta").textContent = `${task.mode === "simulation" ? "模拟数据 · 不可用于实机验收" : "实机数据"} / ${channelLabel(task.baseline.channel)} / ${task.robot_label} / ${task.id}`;
    $("progress").value = Object.keys(task.measurements).length;
    $("baseline").textContent = `${number(task.baseline.distance_mm)} mm`;
    $("verdict").textContent = labels[task.verdict] || task.verdict;
    $("verdict").className = task.verdict.toLowerCase();
    $("task-error").hidden = !task.error; $("task-error").textContent = task.error || "";
    renderRobotDiagnostics(task);
    $("measurements").replaceChildren();
    for (const key of order) {
      const m = task.measurements[key], row = el("tr", null);
      [key, number(m?.reading.distance_mm), number(m?.height_mm), number(m?.deviation_mm), m ? labels[m.verdict] : "待采集"].forEach((v) => row.append(el("td", v)));
      if (m) row.lastChild.className = m.verdict.toLowerCase(); $("measurements").append(row);
    }
    const active = ["RUNNING", "CANCELLING"].includes(task.status);
    $("cancel").disabled = !active || task.status === "CANCELLING";
    $("release").hidden = active || !task.station_locked;
    $("export").disabled = false; $("export-json").disabled = false;
    if (task.station_locked) stationOwner = task.id;
    else if (stationOwner === task.id) stationOwner = null;
    updateStart();
  }
  function renderEvents(events) {
    events.forEach((event) => {
      if (seenEvents.has(event.seq)) return; seenEvents.add(event.seq);
      const data = event.data;
      const description = data.title || data.message || data.key || data.payload?.robotCommands?.[0]?.commandContent?.robotCommandType || data.status || "";
      $("events").append(el("li", `${new Date(event.timestamp).toLocaleTimeString()} ${event.kind} ${description}`));
    });
    while ($("events").children.length > 200) $("events").firstChild.remove();
  }
  async function viewTask(id) {
    currentView = id; generation += 1; const gen = generation; seenEvents = new Set(); $("events").replaceChildren();
    clearTimeout(reconnect); if (taskSocket) { taskSocket.onclose = null; taskSocket.close(); }
    const task = await json(`/tasks/${id}`); if (gen !== generation) return; renderTask(task);
    connectTask(id, gen);
  }
  function clearTaskView(taskId) {
    if (currentView !== taskId) return;
    currentView = null; currentTask = null; generation += 1; seenEvents = new Set();
    clearTimeout(reconnect); if (taskSocket) { taskSocket.onclose = null; taskSocket.close(); taskSocket = null; }
    $("task-title").textContent = "等待标定任务"; $("task-status").textContent = "空闲";
    $("task-meta").textContent = "空载：ALN → AHN → BHN → BLN　负载：BHY → BLY → ALY → AHY";
    $("baseline").textContent = "—"; $("countdown").textContent = "—"; $("verdict").textContent = "待采集";
    $("task-error").hidden = true; $("release").hidden = true;
    $("cancel").disabled = true; $("export").disabled = true; $("export-json").disabled = true;
    $("measurements").replaceChildren();
    for (const key of order) {
      const row = el("tr", null);
      [key, "—", "—", "—", "待采集"].forEach((value) => row.append(el("td", value)));
      $("measurements").append(row);
    }
    $("events").replaceChildren();
  }
  function connectTask(id, gen) {
    const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/calibration/${id}`); taskSocket = ws;
    ws.onmessage = (event) => {
      if (gen !== generation) return;
      const data = JSON.parse(event.data); renderTask(data.task); renderEvents(data.events);
    };
    ws.onclose = (event) => {
      if (stopped || gen !== generation) return;
      if (event.code === 1008) { message("任务连接被拒绝，请检查访问来源和任务是否存在。", true); return; }
      if (currentTask && !["RUNNING", "CANCELLING"].includes(currentTask.status)) { refreshHistory().catch((e) => message(e.message, true)); return; }
      reconnect = setTimeout(() => connectTask(id, gen), 1500);
    };
  }
  $("cancel").addEventListener("click", handle(async () => { renderTask(await json(`/tasks/${currentView}/cancel`, {})); }));
  $("release").addEventListener("click", handle(async () => {
    if (!window.confirm("请现场确认：机器人已完全停止，料箱和工位安全。确认后才允许下一台机器人进入。")) return;
    renderTask(await json(`/tasks/${currentView}/release`, { robot_stopped_and_station_safe: true }));
    message("人工确认已记录，工位已解锁。");
  }));
  function download(blob, filename) {
    const url = URL.createObjectURL(blob), a = el("a", "", { href: url, download: filename });
    document.body.append(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  $("export").addEventListener("click", handle(async () => { download(await (await api(`/tasks/${currentView}/export`)).blob(), `calibration-${currentView}.csv`); }));
  $("export-json").addEventListener("click", handle(async () => {
    const record = await json(`/tasks/${currentView}`); let cursor = 0; const events = [];
    while (true) { const page = await json(`/tasks/${currentView}/events?after=${cursor}`); events.push(...page); if (page.length < 500) break; cursor = page.at(-1).seq; }
    download(new Blob([JSON.stringify({ task: record, events }, null, 2)], { type: "application/json" }), `calibration-${currentView}.json`);
  }));
  $("export-history").addEventListener("click", handle(async () => {
    download(await (await api("/tasks/export")).blob(), "calibration-history.csv");
  }));
  async function refreshHistory() {
    const list = await json("/tasks"); $("history").replaceChildren();
    if (!list.length) {
      const row = el("tr", null), cell = el("td", "暂无历史标定记录", { colspan: "6" });
      row.append(cell); $("history").append(row); return;
    }
    list.forEach((task) => {
      const row = el("tr", null);
      [new Date(task.created_at).toLocaleString(), task.robot_label, task.mode === "simulation" ? "模拟" : "实机", labels[task.status], labels[task.verdict]].forEach((v) => row.append(el("td", v)));
      const cell = el("td", null), actions = el("div", null, { class: "table-actions" });
      const view = el("button", "查看", { type: "button", class: "secondary" });
      const remove = el("button", "删除", { type: "button", class: "danger" });
      view.addEventListener("click", handle(() => viewTask(task.id)));
      remove.addEventListener("click", handle(async () => {
        if (!window.confirm(`确定删除 ${task.robot_label} 于 ${new Date(task.created_at).toLocaleString()} 的标定记录吗？此操作不可恢复。`)) return;
        await api(`/tasks/${encodeURIComponent(task.id)}`, undefined, "DELETE");
        clearTaskView(task.id); await refreshHistory(); message("历史标定记录已删除。");
      }));
      actions.append(view, remove); cell.append(actions); row.append(cell); $("history").append(row);
    });
  }
  $("refresh-history").addEventListener("click", handle(refreshHistory));
  $("clear-history").addEventListener("click", handle(async () => {
    if (!window.confirm("确定清理全部历史标定数据吗？所有任务记录及审计事件将永久删除，且无法恢复。")) return;
    const result = await (await api("/tasks", undefined, "DELETE")).json();
    clearTaskView(currentView); await refreshHistory(); message(`已清理 ${result.deleted} 条历史标定记录。`);
  }));
  async function connect() {
    system = await json("/system"); stationOwner = system.station_owner;
    $("system-status").textContent = system.live_enabled ? "服务在线 · 实机已启用" : "实机未启用 · 请检查 CALIBRATION_LIVE_ENABLED 和 MQTT_HOST";
    await refreshRobots();
    await refreshRecipes();
    if (!currentConfig && recipes.length) loadRecipe(recipes[0].config, recipes[0].id);
    await refreshHistory();
    if (stationOwner) await viewTask(stationOwner);
    updateStart(); message("服务已连接。");
  }
  async function refreshRobots() {
    const selected = $("identity").value;
    const search = $("identity-search");
    const refreshButton = $("refresh-robots");
    search.disabled = true;
    search.placeholder = "正在载入在线机器人…";
    refreshButton.disabled = true;
    $("identity").value = "";
    closeRobotPicker();
    updateStart();
    try {
      const { robot_sns: robotSns } = await json("/robots");
      robotRecords = robotSns.map((robotSn) => ({ sn: robotSn, alias: robotAlias(robotSn) }));
      search.disabled = !robotRecords.length;
      search.placeholder = robotRecords.length ? "输入 SN 或别名，例如 K35、501" : "没有在线机器人";
      if (selected && robotRecords.some((record) => record.sn === selected)) selectRobot(selected);
      else search.value = "";
      updateStart();
    } finally {
      refreshButton.disabled = false;
    }
  }
  function robotAlias(robotSn) {
    const match = /^K3(\d)A(\d{2})AN$/i.exec(robotSn);
    return match ? `${match[1]}${match[2]}` : "";
  }
  function matchingRobots() {
    const query = $("identity-search").value.trim().toUpperCase();
    if (!query) return robotRecords;
    return robotRecords.filter((record) => record.sn.toUpperCase().includes(query) || record.alias.includes(query));
  }
  function closeRobotPicker() {
    robotPickerOpen = false;
    robotPickerActive = -1;
    $("identity-options").hidden = true;
    $("identity-search").setAttribute("aria-expanded", "false");
    $("identity-search").removeAttribute("aria-activedescendant");
  }
  function renderRobotPicker() {
    const options = $("identity-options");
    const matches = matchingRobots();
    if (robotPickerActive >= matches.length) robotPickerActive = -1;
    options.replaceChildren();
    if (!matches.length) {
      options.append(el("div", "没有匹配的在线机器人", { class: "record-select-empty" }));
    } else {
      matches.forEach((record, index) => {
        const option = el("div", null, { id: `identity-option-${index}`, class: `record-select-option${index === robotPickerActive ? " active" : ""}`, role: "option", "aria-selected": String(record.sn === $("identity").value) });
        option.append(el("span", record.sn));
        if (record.alias) option.append(el("small", `别名 ${record.alias}`));
        option.addEventListener("mousedown", (event) => event.preventDefault());
        option.addEventListener("click", () => selectRobot(record.sn));
        options.append(option);
      });
    }
    options.hidden = !robotPickerOpen;
    const search = $("identity-search");
    search.setAttribute("aria-expanded", String(robotPickerOpen));
    if (robotPickerActive >= 0) search.setAttribute("aria-activedescendant", `identity-option-${robotPickerActive}`);
    else search.removeAttribute("aria-activedescendant");
  }
  function openRobotPicker() {
    if ($("identity-search").disabled) return;
    robotPickerOpen = true;
    renderRobotPicker();
  }
  function selectRobot(robotSn) {
    const record = robotRecords.find((item) => item.sn === robotSn);
    if (!record) return;
    $("identity").value = record.sn;
    $("identity-search").value = record.sn;
    closeRobotPicker();
    updateStart();
  }
  $("identity-search").addEventListener("focus", openRobotPicker);
  $("identity-search").addEventListener("input", () => {
    $("identity").value = "";
    robotPickerActive = -1;
    robotPickerOpen = true;
    renderRobotPicker();
    updateStart();
  });
  $("identity-search").addEventListener("keydown", (event) => {
    const matches = matchingRobots();
    if (event.key === "Escape") { closeRobotPicker(); return; }
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      if (!robotPickerOpen) robotPickerOpen = true;
      if (matches.length) robotPickerActive = event.key === "ArrowDown"
        ? (robotPickerActive + 1 + matches.length) % matches.length
        : (robotPickerActive - 1 + matches.length) % matches.length;
      renderRobotPicker();
      return;
    }
    if (event.key === "Enter" && robotPickerOpen) {
      event.preventDefault();
      const record = matches[robotPickerActive >= 0 ? robotPickerActive : 0];
      if (record) selectRobot(record.sn);
    }
  });
  $("identity-search").addEventListener("blur", () => setTimeout(closeRobotPicker, 120));
  $("refresh-robots").addEventListener("click", handle(async () => {
    await refreshRobots();
    message(robotRecords.length ? `已刷新 ${robotRecords.length} 台在线机器人。` : "当前没有在线机器人。");
  }));
  $("connect").addEventListener("click", handle(connect));
  const clock = setInterval(() => {
    if (!currentTask?.wait_until) {
      const waitingStill = currentTask?.status === "RUNNING"
        && currentTask.step?.startsWith("MEASURE_")
        && currentTask.robot_diagnostics
        && !currentTask.robot_diagnostics.stationary;
      $("countdown").textContent = waitingStill ? "等待静止" : "—";
      $("countdown").className = waitingStill ? "waiting" : "";
      return;
    }
    $("countdown").textContent = `${Math.max(0, currentTask.wait_until - Date.now() / 1000).toFixed(1)} s`;
    $("countdown").className = "";
  }, 100);
  for (const key of order) {
    const row = el("tr", null);
    [key, "—", "—", "—", "待采集"].forEach((value) => row.append(el("td", value)));
    $("measurements").append(row);
  }
  function connectSensor() {
    sensorSocket = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/distance`);
    sensorSocket.onmessage = (event) => {
      const data = JSON.parse(event.data), channel = data.channels.find((c) => c.channel === sensorChannel);
      $("sensor-live").textContent = `物理传感器 · ${channelLabel(sensorChannel)}：` + (channel ? (channel.status === "Normal" ? `${number(channel.distance_mm)} mm` : channel.status) : "未配置或无数据");
    };
    sensorSocket.onclose = () => { $("sensor-live").textContent = "物理传感器连接已断开"; if (!stopped) setTimeout(connectSensor, 3000); };
  }
  window.addEventListener("beforeunload", () => { stopped = true; clearInterval(clock); clearTimeout(reconnect); taskSocket?.close(); sensorSocket?.close(); });
  connect().catch((e) => message(e.message, true)); connectSensor();
})();
