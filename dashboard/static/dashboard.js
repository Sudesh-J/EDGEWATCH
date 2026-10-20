/* EDGEWATCH dashboard. Polls /api/state and renders normalized telemetry. */

const POLL_MS = 2500;
const LIVE_WINDOW_MS = 15000;
const PALETTE = ["#5ce7ff", "#7eb0ff", "#3dd68c", "#f5b942", "#d2b4ff"];
const DEDICATED = {
  temperature_c: { canvas: "chart-temperature", empty: "empty-temperature", unit: "°C" },
  humidity_percent: { canvas: "chart-humidity", empty: "empty-humidity", unit: "%" },
};
const SKIP_EXTRA = new Set(["temperature_c", "humidity_percent", "battery_mv"]);

const charts = new Map();
let lastState = null;
let activityFilter = "all";
let lastSummary = "";
let polling = false;

const EVENT_LABELS = {
  mqtt_received: "MQTT message received",
  normalized: "Message normalized",
  azure_success: "Azure send succeeded",
  azure_failed: "Azure send failed",
  invalid_payload: "Invalid payload received",
};

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

function describeKey(key) {
  const suffixes = [
    ["_ug_m3", "µg/m³"],
    ["_percent", "%"],
    ["_mv", "mV"],
    ["_ppm", "ppm"],
    ["_c", "°C"],
  ];
  let unit = "";
  let base = String(key);
  suffixes.some(([suffix, suffixUnit]) => {
    if (base.endsWith(suffix)) {
      unit = suffixUnit;
      base = base.slice(0, -suffix.length);
      return true;
    }
    return false;
  });
  let label;
  if (base.startsWith("pm") && /^\d+$/.test(base.slice(2))) {
    label = `PM${base.slice(2)}`;
  } else if (/^[a-z0-9]+$/i.test(base) && base.length <= 4) {
    label = base.toUpperCase();
  } else {
    label = base.replace(/_/g, " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
  }
  return { key, label, unit };
}

function humanizeType(value) {
  return String(value || "unknown")
    .replace(/_/g, " ")
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function formatNumber(value) {
  if (typeof value !== "number" || Number.isNaN(value)) return String(value);
  if (Number.isInteger(value)) return value.toLocaleString();
  return value.toLocaleString(undefined, { maximumFractionDigits: 2 });
}

function formatLatency(value) {
  if (typeof value !== "number" || Number.isNaN(value)) return "—";
  return value.toLocaleString(undefined, { minimumFractionDigits: 3, maximumFractionDigits: 3 });
}

function formatReading(row) {
  const value = typeof row.value === "number" ? formatNumber(row.value) : String(row.value);
  return row.unit ? `${value} ${row.unit}` : value;
}

function shortTime(iso) {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function fullTime(iso) {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleString(undefined, {
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function ago(iso) {
  if (!iso) return "—";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "—";
  const seconds = Math.max(0, Math.round((Date.now() - then) / 1000));
  if (seconds < 5) return "just now";
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  return `${Math.floor(minutes / 60)}h ago`;
}

function stamp(node, iso, prefix) {
  node.dataset.ts = iso || "";
  node.dataset.prefix = prefix || "";
  node.textContent = iso ? `${prefix || ""}${ago(iso)}` : "—";
  if (iso) node.title = fullTime(iso);
}

function refreshStamps() {
  document.querySelectorAll("[data-ts]").forEach((node) => {
    if (!node.dataset.ts) return;
    node.textContent = `${node.dataset.prefix || ""}${ago(node.dataset.ts)}`;
  });
}

function banner(kind, text) {
  const node = el("p", `banner banner-${kind}`, text);
  node.setAttribute("role", kind === "info" ? "status" : "alert");
  return node;
}

function renderBanners(state) {
  const host = document.getElementById("banners");
  host.replaceChildren();
  const gatewayOnline = Boolean(state.gateway && state.gateway.online);
  const mqtt = state.mqtt || {};
  const azure = state.azure || {};
  const counters = state.counters || {};
  if (state.read_error) host.append(banner("error", state.read_error));
  if (!gatewayOnline) {
    host.append(banner("error", mqtt.status_message || "Gateway is not running."));
  } else if (!mqtt.connected) {
    host.append(banner("error", "MQTT broker unavailable. Device messages cannot arrive until Mosquitto is running."));
  }
  if (azure.mode === "mock") {
    host.append(banner("info", azure.note || "Azure forwarding is in mock mode and is not reaching IoT Hub."));
  } else if (azure.delivery === "blocked") {
    host.append(banner("error", azure.note || "Azure forwarding unavailable."));
  } else if (azure.mode === "live" && counters.azure_failed > 0 && !azure.last_success_at) {
    host.append(banner("warn", "Azure forwarding unavailable. Local MQTT monitoring continues."));
  } else if (azure.mode === "live" && azure.last_error) {
    host.append(banner("warn", `Last Azure send failed: ${azure.last_error}`));
  }
}

function renderSidebar(state) {
  const gatewayOnline = Boolean(state.gateway && state.gateway.online);
  const mqtt = state.mqtt || {};
  const azure = state.azure || {};
  const mqttCard = document.getElementById("mqtt-card");
  const azureCard = document.getElementById("cloud");
  const mqttState = document.getElementById("mqtt-state");
  const mqttDetail = document.getElementById("mqtt-detail");
  const azureState = document.getElementById("azure-state");
  const azureDetail = document.getElementById("azure-detail");

  mqttCard.className = "side-card";
  azureCard.className = "side-card";
  if (!gatewayOnline) {
    mqttCard.classList.add("tone-bad");
    mqttState.textContent = "Gateway offline";
    mqttDetail.textContent = mqtt.status_message || "Start the gateway.";
  } else if (mqtt.connected) {
    mqttCard.classList.add("tone-good");
    mqttState.textContent = "Connected";
    const broker = mqtt.broker ? `${mqtt.broker}:${mqtt.port}` : "localhost";
    mqttDetail.textContent = `Broker ${broker}. Device activity is tracked separately.`;
  } else {
    mqttCard.classList.add("tone-bad");
    mqttState.textContent = "Unavailable";
    mqttDetail.textContent = mqtt.status_message || "MQTT broker unavailable.";
  }

  if (azure.mode === "mock") {
    azureCard.classList.add("tone-warn");
    azureState.textContent = "Not connected";
    azureDetail.textContent = "Mock sender only. Messages are not delivered to IoT Hub.";
  } else if (azure.delivery === "blocked" || azure.mode === "unknown") {
    azureCard.classList.add("tone-bad");
    azureState.textContent = azure.mode === "unknown" ? "No report" : "Unavailable";
    azureDetail.textContent = azure.note || "Azure forwarding is not configured.";
  } else if (azure.last_error && (!azure.last_success_at || azure.last_error_at > azure.last_success_at)) {
    azureCard.classList.add("tone-bad");
    azureState.textContent = "Send failing";
    azureDetail.textContent = azure.last_error || "The SDK send did not succeed.";
  } else if (azure.last_success_at) {
    azureCard.classList.add("tone-good");
    azureState.textContent = "SDK accepted a send";
    azureDetail.textContent = "Confirm the message in Azure IoT Hub. This page does not read from the cloud.";
  } else {
    azureCard.classList.add("tone-idle");
    azureState.textContent = "Configured";
    azureDetail.textContent = "No cloud send has completed yet.";
  }
}

function renderLive(state) {
  const pill = document.getElementById("live-pill");
  const label = document.getElementById("live-text");
  const gatewayOnline = Boolean(state.gateway && state.gateway.online);
  const mqttConnected = Boolean(state.mqtt && state.mqtt.connected && state.mqtt.trusted);
  const last = state.mqtt && state.mqtt.last_message_at ? new Date(state.mqtt.last_message_at).getTime() : 0;
  const fresh = last && Date.now() - last <= LIVE_WINDOW_MS;
  pill.classList.remove("is-live", "is-idle", "is-down");
  if (gatewayOnline && mqttConnected && fresh) {
    pill.classList.add("is-live");
    label.textContent = "Live";
  } else if (gatewayOnline && mqttConnected) {
    pill.classList.add("is-idle");
    label.textContent = "Idle";
  } else {
    pill.classList.add("is-down");
    label.textContent = "Offline";
  }
  stamp(document.getElementById("last-update"), state.updated_at, "");
}

function renderMetrics(state) {
  const host = document.getElementById("metrics");
  const summary = state.summary || { discovered_devices: 0, recently_active: 0, offline_after_seconds: 20 };
  const counters = state.counters || {};
  const azure = state.azure || {};
  const discovered = summary.discovered_devices || 0;
  const active = summary.recently_active || 0;
  const failed = counters.azure_failed || 0;
  const cards = [
    ["Discovered devices", discovered, discovered ? "Seen since the gateway started" : "Waiting for devices", discovered ? "good" : "wait"],
    ["Recently active", active, `Online within ${summary.offline_after_seconds}s`, active ? "good" : "wait"],
    ["MQTT messages received", counters.received || 0, `${counters.invalid || 0} invalid`, "neutral"],
    ["Successful Azure forwards", counters.azure_success || 0, azure.mode === "mock" ? "Mock mode, not cloud delivery" : "SDK send returned", azure.mode === "mock" ? "warn" : "good"],
    ["Failed Azure forwards", failed, failed ? "Check the gateway log" : "No failed sends", failed ? "bad" : "neutral"],
  ];
  host.replaceChildren();
  cards.forEach(([label, value, hint, tone]) => {
    const card = el("article", `metric tone-${tone}`);
    card.append(el("p", "metric-label", label), el("p", "metric-value", formatNumber(value)), el("p", "metric-hint", hint));
    host.append(card);
  });
  host.setAttribute("aria-busy", "false");

  const latency = state.latency || {};
  const analytics = state.analytics || {};
  const insights = document.getElementById("insights");
  const keepTimingOpen = Boolean(insights.querySelector(".definitions") && insights.querySelector(".definitions").open);
  insights.replaceChildren();
  const timing = el("article", "insight");
  timing.append(el("h3", null, "Edge timing"));
  const row = el("div", "stat-row");
  [
    ["Processing", latency.last_processing_ms, "ms last message"],
    ["Forward call", latency.last_forward_ms, "ms last send"],
    ["Payload", latency.last_payload_bytes, "bytes last message"],
  ].forEach(([name, value, unit]) => {
    const block = el("div");
    const shown = value == null ? "—" : name === "Payload" ? formatNumber(value) : formatLatency(value);
    block.append(el("strong", null, shown), el("span", null, `${name} · ${unit}`));
    row.append(block);
  });
  timing.append(row);
  const details = document.createElement("details");
  details.className = "definitions";
  details.open = keepTimingOpen;
  const summaryNode = document.createElement("summary");
  summaryNode.textContent = "What these times mean";
  details.append(summaryNode);
  details.append(el("p", null, latency.processing_definition || "Processing time is measured inside the gateway."));
  details.append(el("p", null, latency.forward_definition || "Forward time is the sender call only."));
  if (latency.avg_processing_ms != null) {
    details.append(el("p", null, `Average processing ${latency.avg_processing_ms} ms. Average forward call ${latency.avg_forward_ms} ms across ${latency.sample_count} normalized messages.`));
  }
  timing.append(details);

  const average = el("article", "insight");
  average.append(el("h3", null, "Temperature average"));
  const windowSize = (analytics.temperature_window || []).length;
  if (analytics.temperature_avg_c == null) {
    average.append(el("p", null, "Waiting for telemetry. The gateway keeps the latest five temperature readings."));
  } else {
    average.append(el("p", null, `${formatNumber(analytics.temperature_avg_c)} °C from the last ${windowSize} of 5 temperature readings normalized at the edge.`));
  }
  insights.append(timing, average);
}

function highlightReading(device) {
  const rows = device.display_readings || [];
  const preferred = rows.find((row) => row.key === "temperature_c" || row.key === "humidity_percent");
  const row = preferred || rows.find((item) => !String(item.key).startsWith("battery_")) || rows[0];
  if (!row || typeof row.value !== "number") return null;
  return row;
}

function historyValues(device, key) {
  return (device.history || [])
    .map((point) => point.readings && point.readings[key])
    .filter((value) => typeof value === "number");
}

function sparkline(values) {
  if (values.length < 2) return null;
  const width = 128;
  const height = 40;
  const min = Math.min(...values);
  const max = Math.max(...values);
  const points = values.map((value, index) => {
    const x = (index / (values.length - 1)) * (width - 2) + 1;
    const ratio = max === min ? 0.5 : (value - min) / (max - min);
    const y = height - 3 - ratio * (height - 8);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(" ");
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.setAttribute("class", "spark");
  svg.setAttribute("aria-hidden", "true");
  const line = document.createElementNS("http://www.w3.org/2000/svg", "polyline");
  line.setAttribute("points", points);
  svg.appendChild(line);
  return svg;
}

function renderSensors(state) {
  const host = document.getElementById("sensor-cards");
  const devices = Object.values(state.devices || {});
  const rule = document.getElementById("device-rule");
  const seconds = state.summary ? state.summary.offline_after_seconds : 20;
  rule.textContent = `Online means a normalized reading arrived within ${seconds} seconds. This is separate from Azure.`;
  if (!devices.length) {
    host.replaceChildren();
    const empty = el("div", "empty-panel");
    empty.append(
      el("h3", null, "Waiting for telemetry"),
      el("p", null, "Start a device simulator. Cards use normalized readings only, so a new device can appear without a dashboard change."),
    );
    host.append(empty);
    return;
  }
  host.replaceChildren();
  devices.sort((a, b) => String(a.device_id).localeCompare(String(b.device_id))).forEach((device) => {
    const card = el("article", "sensor-card");
    const top = el("div", "sensor-top");
    const title = el("div");
    title.append(el("p", "sensor-kicker", humanizeType(device.device_type)));
    const highlight = highlightReading(device);
    const value = el("p", "sensor-value");
    if (!highlight) {
      value.textContent = "Waiting for telemetry";
    } else {
      value.append(document.createTextNode(formatNumber(highlight.value)));
      if (highlight.unit) {
        value.append(document.createTextNode(" "));
        value.append(el("small", null, highlight.unit));
      }
    }
    title.append(value);
    top.append(title);
    const spark = highlight ? sparkline(historyValues(device, highlight.key)) : null;
    if (spark) top.append(spark);
    card.append(top);

    const meta = el("p", "sensor-meta");
    meta.append(el("span", "mono", device.device_id));
    meta.append(document.createTextNode(" · last seen "));
    const seen = el("span");
    stamp(seen, device.last_seen, "");
    meta.append(seen);
    card.append(meta);

    const badge = el("span", device.status === "online" ? "badge badge-online" : "badge badge-offline", device.status === "online" ? "Online" : "Offline");
    card.append(badge);

    const list = el("div", "reading-list");
    (device.display_readings || []).forEach((row) => {
      const line = el("div", "reading-line");
      line.append(el("span", null, row.label), el("strong", null, formatReading(row)));
      list.append(line);
    });
    card.append(list);

    if (device.readings && Object.prototype.hasOwnProperty.call(device.readings, "temperature_c")) {
      const analytics = state.analytics || {};
      if (analytics.temperature_avg_c != null) {
        const count = (analytics.temperature_window || []).length;
        card.append(el("p", "sensor-meta", `Edge average of the last ${count} temperature readings: ${formatNumber(analytics.temperature_avg_c)} °C`));
      }
    }
    host.append(card);
  });
}

function buildAligned(devices, key) {
  const series = [];
  const stamps = new Set();
  Object.values(devices).forEach((device) => {
    const values = new Map();
    (device.history || []).forEach((point) => {
      const reading = point.readings ? point.readings[key] : undefined;
      if (typeof reading === "number" && point.timestamp) {
        values.set(point.timestamp, reading);
        stamps.add(point.timestamp);
      }
    });
    if (values.size) series.push({ id: device.device_id, values });
  });
  const labels = Array.from(stamps).sort().slice(-36);
  const datasets = series.map((entry, index) => ({
    label: entry.id,
    data: labels.map((timestamp) => (entry.values.has(timestamp) ? entry.values.get(timestamp) : null)),
    borderColor: PALETTE[index % PALETTE.length],
    backgroundColor: PALETTE[index % PALETTE.length],
    spanGaps: true,
    tension: 0.3,
    pointRadius: 2.5,
    borderWidth: 2,
    fill: false,
  }));
  return { labels, datasets };
}

function chartOptions(unit) {
  return {
    responsive: true,
    maintainAspectRatio: false,
    animation: false,
    interaction: { mode: "index", intersect: false },
    plugins: {
      legend: {
        labels: { color: "#d5e2f2", boxWidth: 10, boxHeight: 10 },
      },
      tooltip: {
        callbacks: {
          label(context) {
            if (context.parsed.y == null) return context.dataset.label;
            const suffix = unit ? ` ${unit}` : "";
            return `${context.dataset.label}: ${formatNumber(context.parsed.y)}${suffix}`;
          },
        },
      },
    },
    scales: {
      x: {
        ticks: { color: "#b7c7dc", maxRotation: 0, autoSkip: true, maxTicksLimit: 6 },
        grid: { color: "rgba(183, 199, 220, 0.08)" },
      },
      y: {
        ticks: { color: "#b7c7dc" },
        grid: { color: "rgba(183, 199, 220, 0.08)" },
        grace: "12%",
        title: { display: Boolean(unit), text: unit, color: "#b7c7dc" },
      },
    },
  };
}

function updateChart(canvas, emptyNode, devices, key, unit) {
  const aligned = buildAligned(devices, key);
  const hasData = aligned.datasets.length > 0;
  emptyNode.hidden = hasData;
  canvas.parentElement.hidden = !hasData;
  if (!window.Chart || !hasData) return;
  let chart = charts.get(canvas.id);
  if (!chart) {
    chart = new Chart(canvas, { type: "line", data: { labels: [], datasets: [] }, options: chartOptions(unit) });
    charts.set(canvas.id, chart);
  }
  chart.data.labels = aligned.labels.map(shortTime);
  chart.data.datasets = aligned.datasets;
  chart.options.scales.y.title.text = unit;
  chart.update();
}

function extraKeys(devices) {
  const keys = new Set();
  Object.values(devices).forEach((device) => {
    Object.entries(device.readings || {}).forEach(([key, value]) => {
      if (typeof value === "number") keys.add(key);
    });
    (device.history || []).forEach((point) => {
      Object.entries(point.readings || {}).forEach(([key, value]) => {
        if (typeof value === "number") keys.add(key);
      });
    });
  });
  return Array.from(keys).filter((key) => !SKIP_EXTRA.has(key)).sort();
}

function renderExtraCharts(devices) {
  const host = document.getElementById("extra-charts");
  const keys = extraKeys(devices);
  const signature = keys.join("|");
  if (host.dataset.signature !== signature) {
    host.querySelectorAll("canvas").forEach((canvas) => {
      const chart = charts.get(canvas.id);
      if (chart) {
        chart.destroy();
        charts.delete(canvas.id);
      }
    });
    host.replaceChildren();
    keys.forEach((key) => {
      const meta = describeKey(key);
      const safe = key.replace(/[^a-zA-Z0-9_-]/g, "");
      const article = el("article", "panel chart-card");
      const header = el("header", "chart-head");
      header.append(el("h3", null, meta.label), el("span", null, meta.unit || "value"));
      const empty = el("p", "chart-empty", "Waiting for telemetry");
      empty.id = `empty-${safe}`;
      const wrap = el("div", "chart-wrap");
      wrap.hidden = true;
      const canvas = document.createElement("canvas");
      canvas.id = `chart-${safe}`;
      canvas.setAttribute("aria-label", `${meta.label} history`);
      wrap.append(canvas);
      article.append(header, empty, wrap);
      host.append(article);
    });
    host.dataset.signature = signature;
  }
  keys.forEach((key) => {
    const meta = describeKey(key);
    const safe = key.replace(/[^a-zA-Z0-9_-]/g, "");
    const canvas = document.getElementById(`chart-${safe}`);
    const empty = document.getElementById(`empty-${safe}`);
    if (canvas && empty) updateChart(canvas, empty, devices, key, meta.unit);
  });
}

function renderCharts(state) {
  const devices = state.devices || {};
  const note = document.getElementById("chart-library-note");
  note.hidden = Boolean(window.Chart);
  Object.entries(DEDICATED).forEach(([key, target]) => {
    updateChart(
      document.getElementById(target.canvas),
      document.getElementById(target.empty),
      devices,
      key,
      target.unit,
    );
  });
  renderExtraCharts(devices);
}

function renderTable(state) {
  const body = document.getElementById("device-rows");
  const devices = Object.values(state.devices || {}).sort((a, b) => String(a.device_id).localeCompare(String(b.device_id)));
  body.replaceChildren();
  if (!devices.length) {
    const row = document.createElement("tr");
    const cell = el("td", "table-empty", "Waiting for devices");
    cell.colSpan = 5;
    row.append(cell);
    body.append(row);
    return;
  }
  devices.forEach((device) => {
    const row = document.createElement("tr");
    const readings = (device.display_readings || []).map(formatReading).join(" · ") || "Waiting for telemetry";
    const seen = el("span");
    stamp(seen, device.last_seen, "");
    const status = el("span", device.status === "online" ? "badge badge-online" : "badge badge-offline", device.status === "online" ? "Online" : "Offline");
    [el("td", "mono", device.device_id), el("td", null, humanizeType(device.device_type)), el("td", null, readings)].forEach((cell) => row.append(cell));
    const seenCell = document.createElement("td");
    seenCell.append(seen);
    const statusCell = document.createElement("td");
    statusCell.append(status);
    row.append(seenCell, statusCell);
    body.append(row);
  });
}

function renderActivity(state) {
  const list = document.getElementById("activity-list");
  const events = state.activity || [];
  const visible = activityFilter === "all" ? events : events.filter((item) => item.type === activityFilter);
  list.replaceChildren();
  if (!events.length) {
    list.append(el("li", "activity-empty", "No recent activity"));
    return;
  }
  if (!visible.length) {
    list.append(el("li", "activity-empty", "No events of this type"));
    return;
  }
  visible.slice(0, 20).forEach((item) => {
    const li = el("li", `activity-item event-${item.type || "mqtt_received"}`);
    li.append(el("span", "event-dot"));
    const copy = el("div");
    const label = EVENT_LABELS[item.type] || item.type || "Event";
    copy.append(el("p", "activity-copy", `${label}. ${item.detail || ""}`.trim()));
    const time = el("p", "activity-time");
    stamp(time, item.timestamp, "");
    copy.append(time);
    li.append(copy);
    list.append(li);
  });
}

function renderSummary(state) {
  const summary = state.summary || {};
  const text = `${summary.discovered_devices || 0} devices discovered, ${summary.recently_active || 0} recently active.`;
  if (text === lastSummary) return;
  lastSummary = text;
  document.getElementById("live-summary").textContent = text;
}

function renderAll(state) {
  lastState = state;
  renderBanners(state);
  renderSidebar(state);
  renderLive(state);
  renderMetrics(state);
  renderSensors(state);
  renderCharts(state);
  renderTable(state);
  renderActivity(state);
  renderSummary(state);
}

async function poll() {
  if (polling) return;
  polling = true;
  const errorNode = document.getElementById("api-error");
  try {
    const response = await fetch("/api/state", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const state = await response.json();
    errorNode.hidden = true;
    renderAll(state);
  } catch (error) {
    errorNode.hidden = false;
    errorNode.textContent = "Dashboard API unavailable. Start Flask and this page will keep retrying.";
    if (!lastState) {
      document.getElementById("metrics").setAttribute("aria-busy", "false");
      document.getElementById("metrics").replaceChildren(el("p", "loading-copy", "Waiting for the dashboard API."));
    }
  } finally {
    polling = false;
  }
}

function tickClock() {
  document.getElementById("clock").textContent = new Date().toLocaleString(undefined, {
    weekday: "short",
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
  refreshStamps();
  if (lastState) renderLive(lastState);
}

document.querySelectorAll(".filter").forEach((button) => {
  button.addEventListener("click", () => {
    activityFilter = button.dataset.filter;
    document.querySelectorAll(".filter").forEach((item) => item.classList.toggle("is-on", item === button));
    if (lastState) renderActivity(lastState);
  });
});

document.querySelectorAll(".nav-link").forEach((link) => {
  link.addEventListener("click", (event) => {
    const target = document.querySelector(link.getAttribute("href"));
    if (!target) return;
    event.preventDefault();
    target.scrollIntoView({ behavior: "smooth", block: "start" });
    if (target.focus) target.focus({ preventScroll: true });
    document.querySelectorAll(".nav-link").forEach((item) => item.classList.toggle("is-active", item === link));
  });
});

tickClock();
setInterval(tickClock, 1000);
poll();
setInterval(poll, POLL_MS);
