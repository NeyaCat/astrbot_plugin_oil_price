// 全国油价插件配置页
// 通过 window.AstrBotPluginPage bridge 与插件后端 Web API 通信。
// endpoint 为插件内相对路径（不含插件名前缀）。

const bridge = window.AstrBotPluginPage;
const $ = (id) => document.getElementById(id);

let CONFIG = {};
let headerBusy = false;
let OPTIONS = { provinces: [], grades: [] };
let RUNTIME = {};
const multis = {};

// ------------------------------------------------------------------ //
// 通用 UI
// ------------------------------------------------------------------ //
let toastTimer = null;
function toast(message, isError = false) {
  const el = $("toast");
  el.textContent = message;
  el.className = "toast show" + (isError ? " error" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    el.className = "toast";
  }, 3200);
}

function setConnected(text, ok) {
  const el = $("conn");
  el.textContent = text;
  el.className = "tag " + (ok ? "ok" : "bad");
}

// 所有面板的注册表：一个文档级监听统一收起，避免每次重建都挂一份闭包
// （旧实现每渲染一次就 addEventListener 一次，且只认自己那一份 root）。
const multiRegistry = new Map();

function closeAllMultis(exceptId) {
  multiRegistry.forEach((entry, id) => {
    if (id !== exceptId) entry.close();
  });
}

// 点击空白处 / 按 Esc 都能收起 —— 面板是绝对定位浮层，收不起来会一直
// 盖住下方卡片（曾遮住「推送与白名单」整块）。
document.addEventListener("click", (e) => {
  multiRegistry.forEach((entry) => {
    if (!entry.root.contains(e.target)) entry.close();
  });
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeAllMultis();
});

/**
 * 创建一个多选下拉组件。
 *
 * ⚠️ 面板的收起依赖 HTML `hidden` 属性 —— style.css 里必须保留
 * `[hidden] { display: none !important; }`，否则 `.multi-panel{display:flex}`
 * （作者样式）会盖掉 UA 的 `[hidden]{display:none}`，面板永远展开。
 *
 * @param {string} rootId
 * @param {string[]} options
 * @param {string[]} selected
 * @param {{onChange?: () => void}} [opts] 选中项变化时的回调
 * @returns {{get: () => string[]}}
 */
function createMulti(rootId, options, selected, opts = {}) {
  const root = $(rootId);
  root.innerHTML = "";
  const state = new Set(selected || []);
  const boxes = new Map();
  const notify = typeof opts.onChange === "function" ? opts.onChange : null;

  const button = document.createElement("button");
  button.type = "button";
  button.className = "multi-btn";

  const panel = document.createElement("div");
  panel.className = "multi-panel";
  panel.hidden = true;

  const search = document.createElement("input");
  search.className = "multi-search";
  search.placeholder = "搜索…";
  search.type = "text";

  const list = document.createElement("div");
  list.className = "multi-list";

  const allRow = document.createElement("div");
  allRow.className = "multi-actions";
  const btnAll = document.createElement("button");
  btnAll.type = "button";
  btnAll.className = "link";
  btnAll.textContent = "全选";
  const btnNone = document.createElement("button");
  btnNone.type = "button";
  btnNone.className = "link";
  btnNone.textContent = "清空";
  allRow.appendChild(btnAll);
  allRow.appendChild(btnNone);

  const open = () => {
    closeAllMultis(rootId); // 手风琴：同时只开一个，避免两层浮层叠在一起
    panel.hidden = false;
    button.setAttribute("aria-expanded", "true");
    search.value = "";
    list.querySelectorAll(".multi-item").forEach((el) => (el.style.display = ""));
    search.focus();
  };
  const close = () => {
    panel.hidden = true;
    button.setAttribute("aria-expanded", "false");
  };

  function refreshLabel() {
    const arr = [...state];
    button.textContent = arr.length ? arr.join("、") : root.dataset.placeholder || "请选择";
  }

  function commit() {
    refreshLabel();
    if (notify) notify();
  }

  options.forEach((opt) => {
    const item = document.createElement("label");
    item.className = "multi-item";
    item.dataset.opt = opt;
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = state.has(opt);
    cb.addEventListener("change", () => {
      if (cb.checked) state.add(opt);
      else state.delete(opt);
      commit();
    });
    const span = document.createElement("span");
    span.textContent = opt;
    item.appendChild(cb);
    item.appendChild(span);
    list.appendChild(item);
    boxes.set(opt, cb);
  });

  btnAll.addEventListener("click", () => {
    options.forEach((o) => {
      state.add(o);
      if (boxes.has(o)) boxes.get(o).checked = true;
    });
    commit();
  });
  btnNone.addEventListener("click", () => {
    state.clear();
    boxes.forEach((cb) => (cb.checked = false));
    commit();
  });

  search.addEventListener("input", () => {
    const q = search.value.trim();
    list.querySelectorAll(".multi-item").forEach((el) => {
      el.style.display = !q || el.dataset.opt.includes(q) ? "" : "none";
    });
  });

  button.addEventListener("click", (e) => {
    e.stopPropagation();
    if (panel.hidden) open();
    else close();
  });

  panel.appendChild(allRow);
  panel.appendChild(search);
  panel.appendChild(list);
  root.appendChild(button);
  root.appendChild(panel);
  refreshLabel();

  // 同 id 重建时先摘掉旧条目，防止注册表里留下 detached 节点
  multiRegistry.delete(rootId);
  multiRegistry.set(rootId, { root, panel, close });

  return {
    get: () => options.filter((o) => state.has(o)),
    close,
  };
}

// ------------------------------------------------------------------ //
// 数据加载与保存
// ------------------------------------------------------------------ //
/** 调价日基准省份：选项**动态跟随播报地区**，不再手填文本。 */
function refreshRefOptions(preferred) {
  const sel = $("adjust_ref_province");
  if (!sel) return;
  const tracked = (multis.provinces && multis.provinces.get()) || [];
  const list = tracked.length ? tracked.slice() : (OPTIONS.provinces || []).slice();
  const current = preferred || sel.value || CONFIG.adjust_ref_province || "";
  // 配置里存的值可能不在当前播报列表内（手改配置 / 清空省份）：保留在前排，
  // 不至于因为重建 options 把已保存的值静默丢掉。
  if (current && !list.includes(current)) list.unshift(current);

  sel.innerHTML = "";
  list.forEach((p) => {
    const opt = document.createElement("option");
    opt.value = p;
    opt.textContent = p;
    sel.appendChild(opt);
  });
  sel.value = list.includes(current) ? current : list[0] || "";

  const hint = $("ref-hint");
  if (hint) {
    hint.textContent = tracked.length
      ? `选项来自上方「播报地区」（共 ${tracked.length} 个）`
      : "未选择播报地区，暂时列出全部 31 个省级行政区";
  }
}

/** 主题已改为自动（北京时间晨昏点），这里只回显当前判定，不给手动切换。 */
function renderThemeBadge() {
  const el = $("theme-badge");
  if (!el) return;
  const t = RUNTIME.theme || {};
  const isLight = t.theme !== "dark";
  el.innerHTML = "";
  const tag = document.createElement("span");
  tag.className = "tag" + (isLight ? " ok" : "");
  tag.textContent = isLight ? "白天 · light 浅色" : "夜间 · dark 深色";
  el.appendChild(tag);
  const sub = document.createElement("small");
  sub.className = "muted";
  sub.textContent = t.sunrise
    ? `北京时间 ${t.now}　日出 ${t.sunrise} · 日落 ${t.sunset}`
    : "按北京时间自动切换";
  el.appendChild(sub);
}

function fillForm() {
  $("app_key").value = CONFIG.app_key || "";
  $("enable_push").checked = CONFIG.enable_push !== false;
  $("push_time").value = CONFIG.push_time || "07:05";
  $("push_on_adjust").checked = CONFIG.push_on_adjust !== false;
  $("push_users").value = (CONFIG.push_users || []).join("\n");
  $("push_groups").value = (CONFIG.push_groups || []).join("\n");
  $("display_name").value = CONFIG.display_name || "今日油价";

  $("http_api_enable").checked = CONFIG.http_api_enable === true;
  $("http_api_host").value = CONFIG.http_api_host || "0.0.0.0";
  $("http_api_port").value = CONFIG.http_api_port || 9852;

  multis.provinces = createMulti("provinces", OPTIONS.provinces, CONFIG.provinces || [], {
    // 选中省份变了 → 基准省份下拉跟着变（两个字段是主从关系）
    onChange: () => refreshRefOptions(),
  });
  multis.grades = createMulti("grades", OPTIONS.grades, CONFIG.analysis_grades || []);
  refreshRefOptions(CONFIG.adjust_ref_province || "");
  renderThemeBadge();
  renderHttpDoc();
}

// ------------------------------------------------------------------ //
// HTTP 取图接口说明
// ------------------------------------------------------------------ //
/** 0.0.0.0 不可直接访问，展示时替换为当前页面所在主机。 */
function displayHost() {
  const host = $("http_api_host").value.trim() || "0.0.0.0";
  if (host === "0.0.0.0") return location.hostname || "127.0.0.1";
  return host;
}

function renderHttpDoc() {
  const enabled = $("http_api_enable").checked;
  const port = ($("http_api_port").value || "").trim() || "9852";
  const base = `http://${displayHost()}:${port}`;
  const lines = [
    `GET  ${base}/oil`,
    `GET  ${base}/oil?type=report&province=北京&grades=92,95`,
    `GET  ${base}/oil?type=province&province=北京&grade=92`,
    `GET  ${base}/oil?type=calc&province=北京&grade=92&amount=30升`,
    `GET  ${base}/health`,
  ];
  $("http-doc").textContent = lines.join("\n");

  const link = $("http-link");
  link.href = `${base}/oil?type=report`;

  const running = RUNTIME.http_running === true;
  const state = !enabled ? "未开启" : running ? "运行中" : "已开启（未监听，请查看日志）";
  const tag = $("http-state");
  tag.textContent = state;
  tag.className = "tag " + (enabled && running ? "ok" : "bad");
}

/** 把多行文本框拆成字符串数组。 */
function lines(value) {
  return value
    .split("\n")
    .map((x) => x.trim())
    .filter(Boolean);
}

function collectForm() {
  return {
    app_key: $("app_key").value.trim(),
    provinces: multis.provinces.get(),
    analysis_grades: multis.grades.get(),
    enable_push: $("enable_push").checked,
    push_time: $("push_time").value.trim() || "07:05",
    push_on_adjust: $("push_on_adjust").checked,
    push_users: lines($("push_users").value),
    push_groups: lines($("push_groups").value),
    display_name: $("display_name").value.trim() || "今日油价",
    adjust_ref_province: $("adjust_ref_province").value.trim(),
    http_api_enable: $("http_api_enable").checked,
    http_api_host: $("http_api_host").value.trim() || "0.0.0.0",
    http_api_port: Number($("http_api_port").value) || 9852,
  };
}

async function loadConfig() {
  const data = await bridge.apiGet("config");
  CONFIG = data.config || {};
  OPTIONS = data.options || { provinces: [], grades: [] };
  RUNTIME = data.runtime || {};
  fillForm();
  showHeader(await bridge.apiGet("header-background"));
}

async function saveConfig() {
  const payload = collectForm();
  await bridge.apiPost("config", payload);
  CONFIG = { ...CONFIG, ...payload };
  toast("配置已保存");
  // HTTP 服务会在后端异步重启，稍后再取一次运行态刷新提示（顺带刷新主题角标）
  setTimeout(async () => {
    try {
      const data = await bridge.apiGet("config");
      RUNTIME = data.runtime || RUNTIME;
      CONFIG = data.config || CONFIG;
    } catch (err) {
      /* 忽略：仅影响状态角标 */
    }
    renderHttpDoc();
    renderThemeBadge();
  }, 1200);
}

// ------------------------------------------------------------------ //
// 初始化进度（轮询 /status · 局部刷新）
// ------------------------------------------------------------------ //
// 进度面板靠**轮询** `/status` 刷新，而不是推流：
//   - 打开页面就开始轮询，所以「聊天里发 /油价初始化，再打开本页看进度」也能看到；
//   - 每次只改进度条 / 文案 / 日志三处 DOM，整页不重载；
//   - `/status` 返回「最后 200 行 + 累计行数」，按累计行数只追加新增，避免重复刷屏；
//   - 页面不可见时停表，切回来立刻补一次，不在后台空转。
//
// 为什么不用 bridge.subscribeSSE("init-stream")：那条链路只在**点击按钮时**才订阅，
// 且依赖 bridge 的 SSE 能力 —— 实际环境里连不上时整块进度就退化成静态快照。
// 后端端点保留（对外契约），页面不再依赖它。
const PROGRESS_BUSY_MS = 1000; // 运行中的刷新间隔
const PROGRESS_IDLE_MS = 4000; // 空闲时的刷新间隔（为了发现别处启动的初始化）

// 后端 step 是机器码，展示层统一翻成中文。
const STEP_LABELS = {
  start: "准备中",
  prices: "拉取历史油价",
  adjust: "拉取调价事件",
  db: "写入数据库",
  done: "已完成",
};

let progressTimer = null;
let logRun = null; // 已渲染日志所属的 started_at，用来识别「新一轮」
let logSeen = 0; // 已渲染的日志**累计**行数（游标，不是窗口长度）
let initRunning = null; // 上一次已知的运行态（null = 还没拿到过状态）
let statusFailures = 0;
let lastError = null;

/**
 * 渲染日志框：只追加新增行。
 *
 * `/status` 每次返回的是**最后 200 行**的窗口 + 累计行数 `log_count`，
 * 所以游标必须用累计行数（用 logs.length 当游标的话，一旦累计超过窗口长度，
 * 长度恒等于窗口值，会永远切不出新行）。窗口滚掉老行时整段重画。
 */
function renderLogBox(state) {
  const el = $("log");
  const logs = state.logs || [];
  const total = typeof state.log_count === "number" ? state.log_count : logs.length;

  const run = state.started_at || "";
  if (run !== logRun) {
    // 新一轮（started_at 变了）→ 清空重新累计
    logRun = run;
    logSeen = 0;
    el.textContent = "";
  }

  const dropped = total - logs.length; // 已被后端窗口截掉的旧行数
  if (dropped > logSeen) {
    el.textContent = logs.length ? logs.join("\n") + "\n" : "";
    logSeen = total;
    el.scrollTop = el.scrollHeight;
    return;
  }
  if (total <= logSeen) return;

  const atBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 20;
  el.textContent += logs.slice(logSeen - dropped).join("\n") + "\n";
  logSeen = total;
  if (atBottom) el.scrollTop = el.scrollHeight;
}

function setProgress(step, pct) {
  $("step").textContent = step || "空闲";
  $("pct").textContent = (pct || 0) + "%";
  $("bar-fill").style.width = (pct || 0) + "%";
}

function stepText(state) {
  if (state.error) return "失败";
  const label = STEP_LABELS[state.step] || state.step || "空闲";
  if (!state.running && state.step === "done" && state.finished_at) {
    return `${label} · ${String(state.finished_at).slice(5, 16)}`;
  }
  return state.step ? label : "空闲";
}

/** 运行中禁用两个初始化按钮并改文案；结束后弹一次结束提示。 */
function setInitBusy(running) {
  const wasRunning = initRunning;
  initRunning = running;
  $("btn-init").disabled = running;
  $("btn-init-force").disabled = running;
  $("btn-init").textContent = running ? "初始化中…" : "初始化全国历史数据";
  document.body.classList.toggle("init-running", running);
  if (wasRunning === true && !running) toast("初始化流程结束");
}

function renderProgress(state) {
  renderLogBox(state);

  setProgress(stepText(state), state.pct);

  const rows = state.rows;
  $("dbstat").textContent =
    typeof rows === "number" ? `已入库 ${rows} 条 · 覆盖 ${state.days || 0} 个日期` : "";

  if (state.error) {
    if (state.error !== lastError) {
      lastError = state.error;
      toast("初始化失败：" + state.error, true);
    }
  } else {
    lastError = null;
  }

  setInitBusy(state.running === true);
}

function scheduleStatus(delay) {
  clearTimeout(progressTimer);
  progressTimer = setTimeout(pollStatus, delay);
}

async function pollStatus() {
  let state;
  try {
    const data = await bridge.apiGet("status");
    state = data.state || {};
    statusFailures = 0;
  } catch (err) {
    // 连不上不弹一堆提示：连续失败 3 次才说一次，之后继续重试。
    statusFailures += 1;
    if (statusFailures === 3) toast("进度状态读取失败：" + err.message, true);
    scheduleStatus(PROGRESS_IDLE_MS);
    return;
  }
  renderProgress(state);
  scheduleStatus(state.running ? PROGRESS_BUSY_MS : PROGRESS_IDLE_MS);
}

// ------------------------------------------------------------------ //
// 事件绑定
// ------------------------------------------------------------------ //
function showHeader(data) {
  $("header-preview").src = data.image || "";
  $("header-state").textContent = data.custom ? "当前使用自定义背景（三种图片同步生效）" : "当前使用默认雪山";
}

// Normalize locally so the bridge JSON request stays under 1 MB.
async function prepareHeader(file) {
  if (!file || !["image/jpeg", "image/png", "image/webp"].includes(file.type)) {
    throw new Error("请选择 JPEG、PNG 或 WebP 图片");
  }
  if (file.size > 10 * 1024 * 1024) throw new Error("原图不能超过 10 MB");
  const bitmap = await createImageBitmap(file);
  try {
    const ratio = Math.min(1, 1600 / Math.max(bitmap.width, bitmap.height));
    const canvas = document.createElement("canvas");
    canvas.width = Math.max(1, Math.round(bitmap.width * ratio));
    canvas.height = Math.max(1, Math.round(bitmap.height * ratio));
    const ctx = canvas.getContext("2d");
    ctx.fillStyle = "#2a3138";
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
    for (const quality of [0.88, 0.75, 0.6, 0.45]) {
      const data = canvas.toDataURL("image/jpeg", quality);
      if (data.split(",")[1].length <= Math.floor(512 * 1024 / 3) * 4) return data;
    }
    throw new Error("图片压缩后仍超过 512 KB，请缩小图片后重试");
  } finally {
    bitmap.close();
  }
}

async function changeHeader(action) {
  if (headerBusy) return;
  headerBusy = true;
  const buttons = [$("btn-header-upload"), $("btn-header-reset"), $("header-file")];
  buttons.forEach((el) => { el.disabled = true; });
  try {
    const payload = { action };
    if (action === "upload") payload.image = await prepareHeader($("header-file").files[0]);
    const data = await bridge.apiPost("header-background", payload);
    if (data.ok === false) throw new Error(data.error || "背景更新失败");
    showHeader(data);
    $("header-file").value = "";
    toast(action === "reset" ? "已恢复默认雪山" : "页头背景已应用到三种图片");
  } catch (err) {
    toast("背景更新失败：" + err.message, true);
  } finally {
    headerBusy = false;
    buttons.forEach((el) => { el.disabled = false; });
  }
}

function bind() {
  $("btn-header-upload").addEventListener("click", () => changeHeader("upload"));
  $("btn-header-reset").addEventListener("click", () => changeHeader("reset"));
  $("btn-save").addEventListener("click", async () => {
    try {
      await saveConfig();
    } catch (err) {
      toast("保存失败：" + err.message, true);
    }
  });

  // 启动初始化：不在这里订阅任何东西，只发请求 —— 进度由常驻轮询负责刷新。
  async function startInit(force) {
    try {
      const res = await bridge.apiPost("init", { force });
      toast(res.message || "已开始初始化");
      setInitBusy(true); // 立刻反馈，不等下一次轮询
      scheduleStatus(300);
    } catch (err) {
      toast("启动失败：" + err.message, true);
    }
  }

  $("btn-init").addEventListener("click", () => startInit(false));

  $("btn-init-force").addEventListener("click", () => {
    if (
      !confirm(
        "重新初始化会通过东方财富重新拉取今年全国 31 省的全部历史数据并覆盖库中已有记录（不删除数据，仅覆盖同日期同省份同标号），确定继续？",
      )
    )
      return;
    startInit(true);
  });

  $("btn-refresh").addEventListener("click", async () => {
    try {
      const res = await bridge.apiPost("refresh", {});
      toast(`抓取完成，写入 ${res.rows} 条记录`);
    } catch (err) {
      toast("抓取失败：" + err.message, true);
    }
  });

  $("btn-preview").addEventListener("click", async () => {
    const box = $("preview");
    box.innerHTML = '<span class="muted">生成中…</span>';
    try {
      const res = await bridge.apiGet("preview");
      if (res.image_url) {
        box.innerHTML = "";
        const img = document.createElement("img");
        img.src = res.image_url;
        img.alt = "油价预览";
        box.appendChild(img);
      } else {
        box.innerHTML = '<span class="muted">未生成图片（t2i 渲染服务不可用，请在系统配置中启用文本转图片）</span>';
      }
    } catch (err) {
      box.innerHTML = '<span class="muted">预览失败：' + err.message + "</span>";
    }
  });

  ["http_api_enable", "http_api_host", "http_api_port"].forEach((id) => {
    $(id).addEventListener("input", renderHttpDoc);
    $(id).addEventListener("change", renderHttpDoc);
  });
}

// ------------------------------------------------------------------ //
// 启动
// ------------------------------------------------------------------ //
(async function main() {
  bind();
  if (!bridge) {
    setConnected("bridge 不可用", false);
    toast("未检测到页面 bridge，请刷新页面", true);
    return;
  }
  try {
    const ctx = await bridge.ready();
    setConnected("已连接", true);
    await loadConfig();
  } catch (err) {
    setConnected("加载失败", false);
    toast("初始化页面失败：" + err.message, true);
  }
  // 常驻轮询：打开页面就开始，无需先点「初始化」；页面不可见时停表。
  scheduleStatus(0);
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) clearTimeout(progressTimer);
    else scheduleStatus(0);
  });
  window.addEventListener("beforeunload", () => clearTimeout(progressTimer));
})();
