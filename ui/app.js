"use strict";

const $ = (id) => document.getElementById(id);
const token = document.querySelector('meta[name="r3-token"]').content;
const owner = Array.from(crypto.getRandomValues(new Uint8Array(16)), (n) => n.toString(16).padStart(2, "0")).join("");
const online = new Set(["connecting", "ready", "moving", "stopping"]);
const jogButtons = [...document.querySelectorAll("[data-jog]")];
const chineseLabels = {
  heading: "小车控制台", hardware: "R3 麦克纳姆底盘", mode: "控制模式",
  simulation: "模拟", live: "实车", stop: "停车", connectionStatus: "连接状态",
  reconnect: "重开模拟", map: "坐标平面", robot: "小车", target: "目标", pose: "当前位姿",
  x: "X 坐标", y: "Y 坐标", headingAngle: "航向", motion: "运动控制",
  legacyLimits: "当前会话只能用默认限速。", liveConnection: "实车连接",
  ack: "原点已对齐，Mec、Motor ON 后静止，我在主电源旁。", connect: "连接实车", cutoff: "失控时切断主电源。",
  hold: "按住控制", bodyRelative: "相对车头", ccw: "左转", cw: "右转", forward: "前进",
  backward: "后退", left: "左移", right: "右移", legacyJog: "当前会话不支持按住控制。",
  move: "移动", originTitle: "将目标坐标设为 (0, 0)", origin: "原点", targetX: "目标 X", targetY: "目标 Y",
  moveSpeed: "移动限速", moveTo: "移动到坐标", turn: "旋转", headingRelative: "相对当前航向",
  angle: "转角", presets: "常用角度", selectAngle: "选角度", counterclockwise: "逆时针",
  clockwise: "顺时针", turnSpeed: "旋转限速", turnInPlace: "原地旋转", history: "指令记录",
  empty: "暂无已完成指令", time: "时间", command: "指令", final: "结束位姿",
};
const labels = [...document.querySelectorAll("[data-i18n]")].map(node => {
  const attr = node.dataset.i18nAttr;
  return {node, attr, english: attr ? node.getAttribute(attr) : node.textContent};
});
const canvas = $("plot");
let language = "en";
try { if (localStorage.getItem("r3-language") === "zh") language = "zh"; } catch {}
const t = (en, zh) => language === "zh" ? zh : en;
const jogLabel = direction => document.querySelector(`[data-jog="${direction}"] span`)?.textContent || "";

function applyLanguage() {
  document.documentElement.lang = t("en", "zh-CN");
  document.title = t("R3 Control Console", "R3 小车控制台");
  for (const {node, attr, english} of labels) {
    const value = t(english, chineseLabels[node.dataset.i18n]);
    if (attr) node.setAttribute(attr, value);
    else node.textContent = value;
  }
  const button = $("language");
  button.lang = t("zh-CN", "en");
  button.textContent = t("中文", "EN");
  button.title = t("Switch to Chinese", "切换到英文");
  button.setAttribute("aria-label", button.title);
}

let state = {status: "vacant", live: false};
let mode = "sim", sequence = 0, applied = 0;
let connecting = false, submitting = false, initialize = true, polling = false;
let localError = "", view = null, historyKey = "";
let held = null, endingJog = false, gesture = 0;

function translated(message) {
  if (!message) return "";
  if (message.startsWith("ssh: connect to host") || message.startsWith("ssh: Could not resolve hostname")
      || message.startsWith("SSH connection failed:")) {
    return t("Could not reach the Pi. Check its power and network, then reconnect.", "无法连接 Pi 3。请检查 Pi 供电和网络，然后重试。");
  }
  if (message.startsWith("Pi rejected the username or password.")) {
    return t("The Pi rejected the login. Restart the launcher to enter the password again.", "Pi 拒绝了登录。请重开启动脚本并重新输入密码。");
  }
  if (message.startsWith("The Pi does not allow SSH password login.")) {
    return t(message, "Pi 未允许 SSH 密码登录。请在 Pi 上启用，或改用密钥登录。");
  }
  if (message === "Request failed.") return t(message, "请求失败。");
  if (message === "Local control service unavailable. Cut main power if live control is lost.") {
    return t(message, "本机控制服务不可达。实车异常时切断主电源。");
  }
  if (message === "Stop request did not arrive. Cut the robot's main power.") {
    return t(message, "停车请求未送达。请切断实车主电源。");
  }
  if (language === "en") return message;
  if (message.includes("Rotation drifted")) return "转向偏移超过 2 cm，已请求停车。";
  if (message.includes("heartbeat") || message.includes("PC link lost")) return "控制会话已断开，请现场确认车轮静止。";
  if (message.includes("more than 3 m")) return "单次移动最多 3 米。";
  const voltage = message.match(/Board reports ([\d.]+ V) \(expected ([\d.]+-[\d.]+ V)\)/);
  if (voltage) return `C30D 回报 ${voltage[1]}，超出允许范围 ${voltage[2]}。`;
  if (message.includes("Voltage") || message.includes(" V (expected")) return "C30D 电压超出允许范围。";
  if (message.includes("Board reports stop flag")) return "C30D 报告停机，请检查电机使能和板上状态。";
  if (message.includes("timed out") || message.includes("timeout")) return "控制超时，请现场确认车轮静止。";
  if (message.includes("verified C30D UART3") || message.includes("UART3 feedback")) return "没有收到有效的 C30D 回传。";
  if (message.includes("already open")) return "已有控制会话，请先停止。";
  if (message.includes("not ready")) return "控制会话还没就绪。";
  if (message.includes("does not support jog")) return "当前 Pi 会话不支持按住控制，可用坐标移动和定角旋转。";
  if (message.includes("Controller session ended") || message.includes("Controller disconnected")) return "控制会话已结束，请确认车辆静止后重连。";
  if (message.includes("Jog expired")) return "本次按住控制已结束。松手后可重新按下。";
  if (message.includes("Could not confirm stop")) return "未能确认已停车，请关闭黑色主电源。";
  if (message.includes("no motion measured")) {
    const sample = message.match(/vx=(-?\d+) mm\/s, vy=(-?\d+) mm\/s, yaw=(-?\d+) mrad\/s/);
    const values = sample ? `末帧：X ${sample[1]} mm/s，Y ${sample[2]} mm/s，转速 ${sample[3]} mrad/s。` : "";
    return "连续约 2 秒未确认有效轮动，已请求停车。" + values;
  }
  if (message.includes("Host key") || message.includes("host key")) return "Pi 主机密钥未通过核对。";
  return message;
}

async function api(action, values = {}, keepalive = false) {
  const order = ++sequence;
  const response = await fetch(`/api/${action}`, {
    method: "POST", keepalive, signal: keepalive ? undefined : AbortSignal.timeout(1100),
    headers: {"Content-Type": "application/json", "X-R3-Token": token},
    body: JSON.stringify({owner, ...values}),
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || "Request failed.");
  if (order > applied) {
    applied = order;
    if (result.status !== state.status && (result.status === "stopped" || result.status === "fault")) {
      $("ack").checked = false;
    }
    state = result;
    render();
  }
  return result;
}

const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function begin(nextMode, ack = false) {
  if (connecting) return;
  connecting = true;
  localError = "";
  render();
  try {
    if (online.has(state.status)) {
      await api("stop");
      for (let attempt = 0; online.has(state.status) && attempt < 30; attempt++) {
        await delay(90);
        await api("status");
      }
    }
    await api("connect", {mode: nextMode, ack});
  } catch (error) {
    localError = error.message;
  } finally {
    connecting = false;
    render();
  }
}

async function refresh() {
  if (polling) return;
  polling = true;
  try {
    await api("status");
    if (initialize && state.status === "vacant") {
      initialize = false;
      await begin("sim");
    }
  } catch {
    releaseJog();
    localError = "Local control service unavailable. Cut main power if live control is lost.";
    state = {...state, status: "fault", error: ""};
    render();
  } finally {
    polling = false;
  }
}

async function submit(command) {
  if (held || endingJog || submitting || connecting || state.status !== "ready" || state.mode !== mode) return;
  submitting = true;
  localError = "";
  render();
  try {
    await api("command", command);
  } catch (error) {
    localError = error.message;
  } finally {
    submitting = false;
    render();
  }
}

function startJog(button, pointerId, key) {
  if (held || endingJog || button.disabled || document.hidden) return;
  const direction = button.dataset.jog;
  const speed = $(direction === "ccw" || direction === "cw" ? "turn-speed" : "move-speed");
  if (!speed.reportValidity()) return;
  const press = {button, pointerId, key, session: state.jog, gesture: ++gesture};
  held = press;
  localError = "";
  render();
  const send = async (phase) => {
    if (held !== press) return;
    try {
      const result = await api("jog", {phase, session: press.session, gesture: press.gesture,
        ...(phase === "start" ? {direction, speed: speed.valueAsNumber} : {})});
      if (held !== press) return;
      if (result.status === "moving" && result.current?.action === "jog") {
        press.timer = setTimeout(() => void send("hold"), 120);
      } else {
        releaseJog();
      }
    } catch (error) {
      if (held !== press) return;
      localError = error.message;
      releaseJog();
    }
  };
  void send("start");
}

function releaseJog() {
  if (!held) return;
  const press = held;
  held = null;
  endingJog = true;
  clearTimeout(press.timer);
  if (press.pointerId !== null && press.button.hasPointerCapture(press.pointerId)) {
    press.button.releasePointerCapture(press.pointerId);
  }
  render();
  void api("jog", {phase: "end", session: press.session, gesture: press.gesture}, true)
    .catch(() => {}) // A separate short lease stops motion even if this request is lost.
    .finally(() => { endingJog = false; render(); });
}

function statusLabel() {
  const live = state.mode === "live";
  const simulator = t("Local simulation", "本机模拟");
  const console = t("Local console", "本机控制台");
  const checkStop = t("Confirm the robot has stopped", "请现场确认已停车");
  if (state.status === "occupied") return [t("Another tab is in control", "另一标签页正在控制"), t("Return to that tab", "请回到原标签页")];
  if (state.mode === "sim" && mode === "live" && state.status === "ready") return [t("Live not connected", "实车待连接"), t("No live session", "当前没有实车会话")];
  if (state.status === "vacant") return [initialize ? t("Connecting to the simulator", "正在连接模拟器") : t("Not connected", "未连接"), console];
  if (state.status === "connecting") return [t("Connecting", "正在连接"), live ? "Pi 3 → C30D" : simulator];
  if (state.status === "ready") return live
    ? [t("Live connected", "实车已连接"), "Pi 3 → C30D"] : [t("Simulation ready", "模拟待命"), simulator];
  if (state.status === "moving") {
    const action = state.current?.action;
    const motion = action === "jog" ? (held ? jogLabel(state.current.direction) : t("Stopping", "正在停车"))
      : (action === "turn" ? t("Rotating", "正在旋转") : t("Moving", "正在移动"));
    return [motion, live ? t("Live motion", "实车控制中") : simulator];
  }
  if (state.status === "stopping") return [t("Ending session", "正在结束会话"), live ? t("Watch the wheels", "请观察车轮") : simulator];
  if (state.status === "stopped") return live
    ? [t("Live session ended", "实车会话已结束"), checkStop] : [t("Simulation stopped", "模拟已停止"), simulator];
  if (live && !state.pose) return [t("Live not connected", "实车未连接"), t("Reconnect to the Pi", "可重试连接 Pi 3")];
  return [t("Session disconnected", "控制会话断开"), live ? checkStop : console];
}

const decimals = (value, places) => Number.isFinite(value) ? (Math.abs(value) < 10 ** -places / 2 ? 0 : value).toFixed(places) : "—";

function render() {
  const [label, detail] = statusLabel();
  $("link-state").textContent = label;
  $("link-detail").textContent = detail;
  $("state-dot").dataset.state = state.status;
  $("sim").setAttribute("aria-pressed", mode === "sim");
  $("live").setAttribute("aria-pressed", mode === "live");
  $("sim").disabled = connecting;
  $("live").disabled = !state.live || connecting;
  $("stop").disabled = !online.has(state.status) || state.status === "stopping";
  $("reconnect").hidden = mode !== "sim" || online.has(state.status) || state.status === "occupied" || initialize;
  const ready = !held && !endingJog && !connecting && !submitting && state.status === "ready" && state.mode === mode;
  $("move").disabled = !ready;
  $("turn").disabled = !ready;
  $("move-speed").disabled = !ready || !state.speed_limits;
  $("turn-speed").disabled = !ready || !state.speed_limits;
  $("legacy-limits").hidden = !ready || Boolean(state.speed_limits);
  $("legacy-jog").hidden = !ready || Boolean(state.jog);
  for (const button of jogButtons) {
    const pressed = held?.button === button;
    button.toggleAttribute("data-held", pressed);
    button.disabled = !pressed && (!ready || !state.jog);
  }
  $("arm").hidden = mode !== "live" || (state.mode === "live" && online.has(state.status));
  $("connect").disabled = !state.live || !$("ack").checked || connecting || state.status === "occupied";
  const ending = !online.has(state.status) || state.status === "stopping";
  $("notice").textContent = translated(ending && state.error ? state.error : localError || state.error);
  $("notice").hidden = !$("notice").textContent;

  const pose = state.pose;
  $("pose-source").textContent = state.mode === "live"
    ? (state.status === "ready" ? t("Encoder estimate", "编码器估计") : t("Last encoder estimate", "上次编码器估计"))
    : t("Simulated pose", "模拟位置");
  $("pose-x").textContent = decimals(pose?.x_m, 3);
  $("pose-y").textContent = decimals(pose?.y_m, 3);
  $("pose-yaw").textContent = decimals(pose?.yaw_deg, 1);
  renderHistory(state.history || []);
  draw();
}

function renderHistory(history) {
  const key = language + JSON.stringify(history);
  if (historyKey === key) return;
  historyKey = key;
  $("count").textContent = t(`${history.length} ${history.length === 1 ? "command" : "commands"}`, `${history.length} 条`);
  $("empty").hidden = Boolean(history.length);
  $("history-table").hidden = !history.length;
  $("history-body").replaceChildren(...history.slice().reverse().map((result) => {
    const row = document.createElement("tr");
    const {x_m, y_m, yaw_deg} = result.pose;
    let caption = result.action === "jog" ? t("Manual: ", "手动") + jogLabel(result.direction) : (result.action === "turn"
      ? t("Rotate", "旋转") + ` ${result.degrees}°` : t("Move", "移动") + ` (${decimals(result.x, 2)}, ${decimals(result.y, 2)}) m`);
    if (result.speed_deg_s !== undefined) caption += t(", ", "，") + `≤${result.speed_deg_s} °/s`;
    if (result.speed_mm_s !== undefined) caption += t(", ", "，") + `≤${result.speed_mm_s} mm/s`;
    for (const text of [result.time, caption, `(${decimals(x_m, 3)}, ${decimals(y_m, 3)}) m / ${decimals(yaw_deg, 1)}°`]) {
      const cell = document.createElement("td");
      cell.textContent = text;
      row.append(cell);
    }
    return row;
  }));
}

function draw() {
  const width = canvas.clientWidth, height = canvas.clientHeight;
  if (!width || !height) return;
  const ratio = Math.min(devicePixelRatio, 2);
  if (canvas.width !== Math.round(width * ratio) || canvas.height !== Math.round(height * ratio)) {
    canvas.width = Math.round(width * ratio);
    canvas.height = Math.round(height * ratio);
  }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  ctx.fillStyle = "#f9fcfe";
  ctx.fillRect(0, 0, width, height);
  const x0 = Math.round(width / 2) + .5, y0 = Math.round(height / 2) + .5;
  const tx = $("target-x").valueAsNumber, ty = $("target-y").valueAsNumber;
  const target = [tx, ty].every(Number.isFinite) && Math.max(Math.abs(tx), Math.abs(ty)) <= 100;
  const poses = (state.history || []).slice(-8).map((entry) => entry.pose);
  const p = state.pose;
  const extent = Math.max(.4, ...[...poses, p].filter(Boolean).flatMap((v) => [Math.abs(v.x_m), Math.abs(v.y_m)]),
    ...(target ? [Math.abs(tx), Math.abs(ty)] : [])) + .12;
  const scale = (Math.min(width, height) / 2 - 32) / extent;
  const step = extent <= .6 ? .1 : extent <= 1.5 ? .25 : extent <= 3 ? .5 : extent <= 8 ? 1 : extent <= 24 ? 5 : 20;
  const px = (x) => x0 + scale * x, py = (y) => y0 - scale * y;
  view = {x0, y0, scale};

  function line(ax, ay, bx, by, color, thickness = 1) {
    ctx.beginPath();
    ctx.moveTo(ax, ay);
    ctx.lineTo(bx, by);
    ctx.strokeStyle = color;
    ctx.lineWidth = thickness;
    ctx.stroke();
  }

  ctx.font = '11px "Cascadia Mono", Consolas, monospace';
  ctx.fillStyle = "#566e82";
  for (let i = -Math.ceil(width / (2 * scale * step)); i <= Math.ceil(width / (2 * scale * step)); i++) {
    if (!i) continue;
    const x = px(i * step);
    line(x, 0, x, height, "#deebf2");
    if ((Math.abs(i) % 2 === 0 || scale * step > 47) && x > 24 && x < width - 32) {
      ctx.textAlign = "center";
      ctx.fillText(decimals(i * step, step < 1 ? 2 : 0), x, y0 + 15);
    }
  }
  for (let i = -Math.ceil(height / (2 * scale * step)); i <= Math.ceil(height / (2 * scale * step)); i++) {
    if (!i) continue;
    const y = py(i * step);
    line(0, y, width, y, "#deebf2");
    if ((Math.abs(i) % 2 === 0 || scale * step > 47) && y > 28 && y < height - 17) {
      ctx.textAlign = "right";
      ctx.fillText(decimals(i * step, step < 1 ? 2 : 0), x0 - 7, y + 4);
    }
  }
  line(13, y0, width - 14, y0, "#839daf");
  line(x0, height - 14, x0, 14, "#839daf");
  ctx.font = '600 12px "Cascadia Mono", Consolas, monospace';
  ctx.textAlign = "left";
  ctx.fillStyle = "#38556b";
  ctx.fillText("+Y", x0 + 8, 23);
  ctx.fillText("+X", width - 30, y0 - 8);
  ctx.fillText("0", x0 + 8, y0 + 15);

  let prev = {x_m: 0, y_m: 0};
  for (const pose of poses) {
    line(px(prev.x_m), py(prev.y_m), px(pose.x_m), py(pose.y_m), "#8faec3", 1.5);
    prev = pose;
  }
  if (target) {
    const x = px(tx), y = py(ty);
    ctx.save();
    ctx.setLineDash([4, 4]);
    line(x0, y0, x, y, "#648f83");
    ctx.restore();
    ctx.beginPath();
    ctx.moveTo(x, y - 7); ctx.lineTo(x + 7, y); ctx.lineTo(x, y + 7); ctx.lineTo(x - 7, y);
    ctx.closePath();
    ctx.fillStyle = "#e7f5ee";
    ctx.fill();
    ctx.lineWidth = 2;
    ctx.strokeStyle = "#1b755c";
    ctx.stroke();
  }
  if (p) {
    ctx.save();
    ctx.translate(px(p.x_m), py(p.y_m));
    ctx.rotate(-p.yaw_deg * Math.PI / 180);
    ctx.fillStyle = "#145f9a";
    ctx.beginPath();
    ctx.roundRect(-10, -11, 22, 22, 3);
    ctx.fill();
    ctx.beginPath();
    ctx.moveTo(16, 0); ctx.lineTo(7, -6); ctx.lineTo(7, 6);
    ctx.closePath();
    ctx.fill();
    line(-4, 0, 5, 0, "#fff", 2);
    ctx.restore();
  }
}

$("language").addEventListener("click", () => {
  language = t("zh", "en");
  try { localStorage.setItem("r3-language", language); } catch {}
  applyLanguage();
  render();
});
$("sim").addEventListener("click", () => {
  releaseJog();
  mode = "sim";
  $("ack").checked = false;
  if (state.mode !== "sim" && state.status !== "occupied") void begin("sim");
  render();
});
$("live").addEventListener("click", () => {
  releaseJog();
  mode = "live";
  $("ack").checked = false;
  render();
});
$("ack").addEventListener("change", () => {
  render();
});
$("connect").addEventListener("click", () => {
  if ($("ack").checked) void begin("live", true);
});
$("reconnect").addEventListener("click", () => void begin("sim"));
$("stop").addEventListener("click", async () => {
  releaseJog();
  try { await api("stop"); } catch { localError = "Stop request did not arrive. Cut the robot's main power."; render(); }
});
$("move-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const cmd = {action: "goto", x: $("target-x").valueAsNumber, y: $("target-y").valueAsNumber};
  if (state.speed_limits) cmd.speed_mm_s = $("move-speed").valueAsNumber;
  void submit(cmd);
});
$("turn-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const cmd = {action: "turn", degrees: $("degrees").valueAsNumber};
  if (state.speed_limits) cmd.speed_deg_s = $("turn-speed").valueAsNumber;
  void submit(cmd);
});
$("origin").addEventListener("click", () => { $("target-x").value = "0"; $("target-y").value = "0"; draw(); });
for (const input of [$("target-x"), $("target-y")]) input.addEventListener("input", draw);
$("angle-preset").addEventListener("change", () => {
  const value = $("angle-preset").value;
  if (value) $("degrees").value = value;
});
$("degrees").addEventListener("input", () => { $("angle-preset").value = ""; });
canvas.addEventListener("click", (event) => {
  if (!view) return;
  const rect = canvas.getBoundingClientRect();
  $("target-x").value = decimals((event.clientX - rect.left - view.x0) / view.scale, 2);
  $("target-y").value = decimals((rect.top + view.y0 - event.clientY) / view.scale, 2);
  draw();
});
for (const button of jogButtons) {
  button.addEventListener("pointerdown", (event) => {
    if (event.button !== 0 || !event.isPrimary || held || endingJog) return;
    event.preventDefault();
    button.focus({preventScroll: true});
    button.setPointerCapture(event.pointerId);
    startJog(button, event.pointerId, null);
  });
  button.addEventListener("pointermove", (event) => {
    if (held?.pointerId !== event.pointerId) return;
    const rect = button.getBoundingClientRect();
    if ((event.buttons & 1) === 0 || event.clientX < rect.left || event.clientX >= rect.right
        || event.clientY < rect.top || event.clientY >= rect.bottom) releaseJog();
  });
  for (const name of ["pointerleave", "pointercancel", "lostpointercapture"]) {
    button.addEventListener(name, (event) => {
      if (held?.pointerId === event.pointerId) releaseJog();
    });
  }
  button.addEventListener("contextmenu", (event) => { event.preventDefault(); releaseJog(); });
  button.addEventListener("keydown", (event) => {
    if (event.code !== "Space" && event.code !== "Enter") return;
    event.preventDefault();
    if (!event.repeat) startJog(button, null, event.code);
  });
  button.addEventListener("blur", () => { if (held?.button === button) releaseJog(); });
}
addEventListener("pointerup", (event) => { if (held?.pointerId === event.pointerId) releaseJog(); });
addEventListener("keyup", (event) => { if (held?.key === event.code) releaseJog(); });
addEventListener("keydown", (event) => { if (event.key === "Escape") releaseJog(); });
for (const event of ["blur", "scroll", "resize", "offline"]) addEventListener(event, releaseJog);
function dropLink() {
  releaseJog();
  if (online.has(state.status)) api("stop", {}, true).catch(() => {});
}
addEventListener("pagehide", dropLink);
document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    if (state.mode === "live") dropLink();
    else releaseJog();
  }
});
applyLanguage();
new ResizeObserver(draw).observe(canvas);
setInterval(refresh, 250);
void refresh();
