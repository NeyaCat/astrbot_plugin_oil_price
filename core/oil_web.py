"""HTTP 取图服务的 Web 查询页（v2.8.0）。

职责单一：**产出一个自包含的 HTML 字符串**。路由、出图、参数校验仍在
``oil_http.py`` —— 那个文件已经够长，页面（HTML + CSS + JS 上千行）塞进去
会彻底盖住服务端逻辑，所以单独放这里。

三条硬约束（都是为了「能被反向代理到域名」这件事）
--------------------------------------------------
1. **相对路径**：页面里所有请求都拼在「当前文档所在目录」下面
   （``location.pathname`` 去掉最后一段），不出现 ``http://`` 或写死端口。
   这样 ``http://10.0.0.2:9852/`` 被反代到 ``https://example.com/oil/`` 时，
   页面会去请求 ``https://example.com/oil/oil?...``，nginx 剥掉 `/oil` 前缀即可，
   页面**完全不用改**。
2. **零外部依赖**：不引 CDN、不引外部字体/图标，断网也能用（这玩意儿常常
   跑在内网机器上）。
3. **选项服务端注入**：省份 / 标号 / 城市别名以 ``<script type="application/json">``
   的形式直接写进页面，页面不再多发一次取选项的请求（少一次往返，也少一处能坏的地方）。

页面只用两个接口，都是既有的：

- 多选地区 + 多选油号 → ``GET {base}oil?type=report&provinces=北京,上海&grades=92#,95#``
- 单选地区 + 单选油号 + 数量 → ``GET {base}oil?type=calc&province=北京&grade=92&amount=30升``
"""

from __future__ import annotations

import json

from .config import ALL_GRADES, CITY_ALIASES, PROVINCES

__all__ = ["build_options", "render_page"]

# 页面里的占位符：被替换成选项 JSON。用占位符而不是 str.format，
# 因为页面里满是 CSS 的 `{}`，format 会把它们全当成字段。
_OPTIONS_SLOT = "__OIL_OPTIONS_JSON__"


def build_options(star) -> dict:
    """收集页面要用的下拉选项。

    - ``provinces``：全国 31 个省级行政区（接口本来就收任意省份）；
    - ``grades``：全部可选标号（``92#`` 形式，与库里、与图上的写法一致）；
    - ``aliases``：``{省: [城市...]}`` 反查表，让下拉框里输入「深圳」也能搜到「广东」；
    - ``defaultProvinces`` / ``defaultGrades``：插件当前配置的播报地区与标号，
      页面默认帮用户勾上（别让人一进来面对 31 个省自己挑）。
    """
    aliases: dict[str, list[str]] = {}
    for city, province in CITY_ALIASES.items():
        aliases.setdefault(province, []).append(city)

    province_set = set(PROVINCES)
    grade_set = set(ALL_GRADES)
    current_provinces = [p for p in (getattr(star, "provinces", None) or []) if p in province_set]
    current_grades = [
        g for g in (getattr(star, "analysis_grades", None) or []) if g in grade_set
    ]

    return {
        "provinces": list(PROVINCES),
        "grades": list(ALL_GRADES),
        "aliases": {k: sorted(v) for k, v in aliases.items()},
        "defaultProvinces": current_provinces,
        "defaultGrades": current_grades,
        "version": str(getattr(star, "plugin_version", "") or ""),
    }


def render_page(star) -> str:
    """渲染页面 HTML（选项直接内联，无需二次请求）。"""
    payload = json.dumps(build_options(star), ensure_ascii=False, separators=(",", ":"))
    # 防止 JSON 里的 `</script>` 提前闭合脚本块（省份/别名里虽然不会有，
    # 但别把安全性寄托在"数据长得善良"上）。
    payload = payload.replace("</", "<\\/")
    return PAGE_TEMPLATE.replace(_OPTIONS_SLOT, payload)


PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>油价查询</title>
<style>
:root{
  --bg:#f5f3ef; --card:#fff; --ink:#1f2328; --ink-2:#575e6a; --ink-3:#8a909a;
  --line:#e5e1d9; --line-2:#efece6;
  --amber:#8a5200; --amber-2:#b06a00; --amber-bg:#fdf4e3; --amber-line:#f0dcb4;
  --up:#d93025; --down:#188038; --shadow:0 1px 2px rgba(24,20,12,.06),0 8px 24px rgba(24,20,12,.05);
}
@media (prefers-color-scheme: dark){
  :root{
    --bg:#14171c; --card:#1c2027; --ink:#eef1f5; --ink-2:#aeb6c2; --ink-3:#7d8694;
    --line:#2b3138; --line-2:#242a31;
    --amber:#f0b95c; --amber-2:#f4c877; --amber-bg:#2a2416; --amber-line:#4a3d21;
    --shadow:0 1px 2px rgba(0,0,0,.4),0 8px 24px rgba(0,0,0,.35);
  }
}
*{box-sizing:border-box}
html,body{margin:0;padding:0}
body{
  background:var(--bg); color:var(--ink);
  font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;
  -webkit-text-size-adjust:100%;
}
.wrap{max-width:880px;margin:0 auto;padding:28px 18px 56px}
header.top{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;margin-bottom:4px}
h1{font-size:24px;margin:0;letter-spacing:.5px}
.ver{font-size:12px;color:var(--ink-3);font-variant-numeric:tabular-nums}
.lead{color:var(--ink-2);font-size:13px;margin:6px 0 18px}
.tabs{display:flex;gap:8px;margin:0 0 14px;border-bottom:1px solid var(--line);padding-bottom:0}
.tab{
  appearance:none;border:0;background:none;font:inherit;font-weight:600;color:var(--ink-3);
  padding:9px 4px;margin-bottom:-1px;border-bottom:2px solid transparent;cursor:pointer;
}
.tab[aria-selected="true"]{color:var(--amber);border-bottom-color:var(--amber)}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:18px;box-shadow:var(--shadow)}
.card + .card{margin-top:14px}
.field{margin-bottom:14px}
.field > label{display:block;font-size:13px;color:var(--ink-2);margin-bottom:6px;font-weight:600}
.hint{font-size:12px;color:var(--ink-3);font-weight:400;margin-left:6px}
.row{display:flex;gap:10px;flex-wrap:wrap}
.row > *{min-width:0}
.picker{position:relative}
.pk-field{
  display:flex;flex-wrap:wrap;gap:6px;align-items:center;min-height:44px;
  padding:6px 10px;border:1px solid var(--line);border-radius:10px;background:var(--card);
  cursor:text;
}
.pk-field.is-open,.pk-field:focus-within{border-color:var(--amber-2);box-shadow:0 0 0 3px var(--amber-bg)}
.pk-input{flex:1 1 90px;min-width:80px;border:0;outline:0;background:none;font:inherit;color:var(--ink);padding:4px 0}
.pk-input::placeholder{color:var(--ink-3)}
.chip{
  display:inline-flex;align-items:center;gap:5px;background:var(--amber-bg);color:var(--amber);
  border:1px solid var(--amber-line);border-radius:999px;padding:2px 6px 2px 10px;font-size:13px;
  font-weight:600;line-height:1.5;white-space:nowrap;
}
.chip button{appearance:none;border:0;background:none;color:inherit;font-size:15px;line-height:1;cursor:pointer;padding:0 2px;opacity:.7}
.chip button:hover{opacity:1}
.pk-menu{
  position:absolute;z-index:30;left:0;right:0;top:calc(100% + 6px);max-height:264px;overflow:auto;
  background:var(--card);border:1px solid var(--line);border-radius:12px;box-shadow:var(--shadow);padding:6px;
}
.pk-menu[hidden]{display:none}
.pk-item{display:flex;align-items:baseline;gap:8px;padding:8px 10px;border-radius:8px;cursor:pointer}
.pk-item:hover,.pk-item.is-active{background:var(--amber-bg)}
.pk-item .nm{font-weight:600}
.pk-item .ax{font-size:12px;color:var(--ink-3)}
.pk-item.is-on .nm::before{content:"✓ ";color:var(--amber)}
.pk-empty{padding:10px;color:var(--ink-3);font-size:13px;text-align:center}
.pk-tools{display:flex;gap:8px;margin-left:auto}
.linkish{appearance:none;border:0;background:none;color:var(--amber-2);font:inherit;font-size:12px;cursor:pointer;padding:2px 0}
.linkish:hover{text-decoration:underline}
.btn{
  appearance:none;border:1px solid transparent;border-radius:10px;font:inherit;font-weight:700;
  padding:11px 22px;cursor:pointer;background:var(--amber);color:#fff;
}
.btn:disabled{opacity:.55;cursor:not-allowed}
.btn.ghost{background:transparent;color:var(--amber-2);border-color:var(--amber-line)}
input[type=number],select{
  font:inherit;color:var(--ink);background:var(--card);border:1px solid var(--line);
  border-radius:10px;padding:10px 12px;
}
input[type=number]{width:130px}
input[type=number]:focus,select:focus{outline:0;border-color:var(--amber-2);box-shadow:0 0 0 3px var(--amber-bg)}
.msg{margin-top:14px;font-size:13px;border-radius:10px;padding:10px 12px;display:none}
.msg.is-err{display:block;background:#fdecea;color:#8c1d18;border:1px solid #f5c6c2}
.msg.is-info{display:block;background:var(--amber-bg);color:var(--amber);border:1px solid var(--amber-line)}
@media (prefers-color-scheme: dark){
  .msg.is-err{background:#3a1d1b;color:#f6b0aa;border-color:#5c2b27}
}
.result{margin-top:16px;display:none}
.result.is-on{display:block}
.result img{display:block;width:100%;height:auto;border:1px solid var(--line);border-radius:12px;background:var(--card)}
.result-bar{display:flex;gap:14px;align-items:center;margin-top:8px;font-size:12px;color:var(--ink-3);flex-wrap:wrap}
.result-bar a{color:var(--amber-2)}
footer.foot{margin-top:26px;font-size:12px;color:var(--ink-3);line-height:1.9}
footer.foot code{background:var(--line-2);border-radius:5px;padding:1px 5px;font-size:11px}
@media (max-width:560px){
  .wrap{padding:20px 14px 44px}
  h1{font-size:20px}
  .btn{width:100%}
}
</style>
</head>
<body>
<div class="wrap">

  <header class="top">
    <h1>油价查询</h1>
    <span class="ver" id="ver"></span>
  </header>
  <p class="lead">下拉框里可以直接打字搜索（输入「深圳」也能找到「广东」）。选完点按钮，出图后长按或右键保存即可。</p>

  <div class="tabs" role="tablist">
    <button class="tab" id="tab-report" role="tab" aria-selected="true" aria-controls="panel-report">查油价 · 多选</button>
    <button class="tab" id="tab-calc" role="tab" aria-selected="false" aria-controls="panel-calc">算油费 · 单选</button>
  </div>

  <section class="card" id="panel-report" role="tabpanel" aria-labelledby="tab-report">
    <div class="field">
      <label>地区<span class="hint">可多选，出在一张图上</span></label>
      <div id="pk-provinces"></div>
    </div>
    <div class="field">
      <label>油号<span class="hint">可多选</span></label>
      <div id="pk-grades"></div>
    </div>
    <button class="btn" id="btn-report" type="button">查询油价</button>
    <div class="msg" id="msg-report"></div>
    <div class="result" id="res-report">
      <img id="img-report" alt="油价图">
      <div class="result-bar">
        <a id="open-report" href="#" target="_blank" rel="noopener">在新标签打开原图</a>
        <button class="linkish" id="copy-report" type="button">复制图片直链</button>
        <span>链接可以直接贴到 Markdown / 群里</span>
      </div>
    </div>
  </section>

  <section class="card" id="panel-calc" role="tabpanel" aria-labelledby="tab-calc" hidden>
    <div class="field">
      <label>地区<span class="hint">单选</span></label>
      <div id="pk-calc-province"></div>
    </div>
    <div class="field">
      <label>油号<span class="hint">单选</span></label>
      <div id="pk-calc-grade"></div>
    </div>
    <div class="field">
      <label>数量<span class="hint">填升数算花多少钱，填金额算能加多少升</span></label>
      <div class="row">
        <input type="number" id="calc-qty" min="0.01" step="1" value="30" inputmode="decimal" aria-label="数量">
        <select id="calc-unit" aria-label="单位">
          <option value="升">升</option>
          <option value="元">元</option>
        </select>
      </div>
    </div>
    <button class="btn" id="btn-calc" type="button">算一算</button>
    <div class="msg" id="msg-calc"></div>
    <div class="result" id="res-calc">
      <img id="img-calc" alt="结算单">
      <div class="result-bar">
        <a id="open-calc" href="#" target="_blank" rel="noopener">在新标签打开原图</a>
        <button class="linkish" id="copy-calc" type="button">复制图片直链</button>
      </div>
    </div>
  </section>

  <footer class="foot">
    数据来源：ShowAPI 138-46 / 138-49 · 历史价格来自东方财富 · 仅供参考，以加油站挂牌价为准。<br>
    这些接口也可以程序化调用：<code>oil?type=report&amp;provinces=北京,上海&amp;grades=92#,95#</code>、
    <code>oil?type=calc&amp;province=北京&amp;grade=92&amp;amount=30升</code>；
    其它端点见 <a href="help">help</a>、<a href="health">health</a>。
  </footer>

</div>

<script id="oil-options" type="application/json">__OIL_OPTIONS_JSON__</script>
<script>
(function () {
  "use strict";

  var OPT = {};
  try {
    OPT = JSON.parse(document.getElementById("oil-options").textContent || "{}");
  } catch (e) { OPT = {}; }

  /* ------------------------------------------------------------------ *
   * 相对路径：所有请求都挂在「当前文档所在目录」下面。
   * 这样反向代理把本服务挂到任意域名 / 任意路径前缀下，页面都零改动。
   * ------------------------------------------------------------------ */
  var BASE = location.pathname.slice(-1) === "/"
    ? location.pathname
    : location.pathname.replace(/[^/]*$/, "");

  function oilUrl(params) {
    var parts = [];
    Object.keys(params).forEach(function (k) {
      var v = params[k];
      if (v === undefined || v === null || v === "") { return; }
      parts.push(encodeURIComponent(k) + "=" + encodeURIComponent(v));
    });
    return BASE + "oil" + (parts.length ? "?" + parts.join("&") : "");
  }

  /* ------------------------------------------------------------------ *
   * 记住上次的选择：写进浏览器 localStorage，下次打开直接回显，不用重新点一遍。
   *
   * 三条纪律：
   * 1) 读写全程 try/catch —— 隐私模式 / 禁用站点存储时 localStorage 会直接抛异常，
   *    不能因为存不了就把整页 JS 打断，退化成「这次不记忆」即可。
   * 2) 回显要**按当前可用选项过滤** —— 油号可能变（比如 98# 没了），
   *    照旧值回显会留下一个选不中也删不掉的芯片。
   * 3) 存的只是**选择**（地区/油号/数量/标签），不含任何请求结果。
   * ------------------------------------------------------------------ */
  var STORE_KEY = "oil-query-v1";

  function loadState() {
    try {
      var obj = JSON.parse(window.localStorage.getItem(STORE_KEY) || "{}");
      return (obj && typeof obj === "object") ? obj : {};
    } catch (e) { return {}; }
  }

  function saveState(patch) {
    try {
      var cur = loadState();
      Object.keys(patch).forEach(function (k) { cur[k] = patch[k]; });
      window.localStorage.setItem(STORE_KEY, JSON.stringify(cur));
    } catch (e) { /* 存不了就算了，不影响查询 */ }
  }

  // 列表：没存过（不是数组）→ 回落默认值；存过（哪怕是用户故意清空的 []）→
  // 尊重用户的选择，只把已失效的项丢掉。
  function pickList(savedList, allowed, fallback) {
    if (!Array.isArray(savedList)) { return (fallback || []).slice(); }
    var known = {};
    (allowed || []).forEach(function (v) { known[v] = true; });
    return savedList.filter(function (v) { return known[v]; });
  }

  // 单选：存入的是字符串，回显时包装成 0/1 元素的数组。
  // ⚠️ 必须返回**数组** —— Picker 的 value 契约是数组（内部 `(cfg.value || []).slice()`
  // 然后 forEach / indexOf）。返回裸字符串会被 `slice()` 变成字符串，让 renderTags
  // 抛 `selected.forEach is not a function`，整个 IIFE 中断（按钮和标签页都绑不上）。
  // 区分「从没存过」(undefined → 用默认值) 与「用户故意清空」("" → 保持空) ——
  // 否则用户清空后重开页面又冒出来，看着像 bug。
  function pickOne(saved, allowed, fallback) {
    var chosen = null;
    if (saved === undefined) {
      chosen = (fallback === undefined) ? null : fallback;
    } else {
      var list = pickList([saved], allowed, []);
      if (list.length) { chosen = list[0]; }
      else if (saved !== "") { chosen = (fallback === undefined) ? null : fallback; }
    }
    return chosen ? [chosen] : [];
  }

  var SAVED = loadState();
  var SAVED_REPORT = SAVED.report || {};
  var SAVED_CALC = SAVED.calc || {};

  var versionEl = document.getElementById("ver");
  if (versionEl && OPT.version) { versionEl.textContent = "v" + OPT.version; }

  /* ------------------------------------------------------------------ *
   * 下拉选择器：可打字搜索、可多选。
   * 原生 <select> 没有搜索、也不能多选芯片，所以自己撸一个小的。
   * ------------------------------------------------------------------ */
  function Picker(host, cfg) {
    var multi = !!cfg.multi;
    var options = cfg.options || [];        // [{value, label, aliases:[...]}]
    var selected = (cfg.value || []).slice();
    var open = false;
    var active = -1;
    var shown = [];

    var field = document.createElement("div");
    field.className = "pk-field";
    var tags = document.createElement("span");
    tags.style.display = "contents";
    var input = document.createElement("input");
    input.className = "pk-input";
    input.type = "text";
    input.setAttribute("autocomplete", "off");
    input.placeholder = cfg.placeholder || "搜索…";
    field.appendChild(tags);
    field.appendChild(input);

    var menu = document.createElement("div");
    menu.className = "pk-menu";
    menu.hidden = true;

    host.textContent = "";
    host.className = "picker";
    host.appendChild(field);
    host.appendChild(menu);

    function byValue(v) {
      for (var i = 0; i < options.length; i++) {
        if (options[i].value === v) { return options[i]; }
      }
      return null;
    }

    function renderTags() {
      tags.textContent = "";
      selected.forEach(function (v) {
        var o = byValue(v);
        if (!o) { return; }
        var chip = document.createElement("span");
        chip.className = "chip";
        chip.appendChild(document.createTextNode(o.label));
        var x = document.createElement("button");
        x.type = "button";
        x.setAttribute("aria-label", "移除 " + o.label);
        x.textContent = "×";
        x.addEventListener("mousedown", function (ev) {
          ev.preventDefault();
          ev.stopPropagation();
          pick(v);
        });
        chip.appendChild(x);
        tags.appendChild(chip);
      });
      input.placeholder = selected.length ? "" : (cfg.placeholder || "搜索…");
    }

    function renderMenu() {
      var q = input.value.trim().toLowerCase();
      menu.textContent = "";
      shown = [];
      options.forEach(function (o) {
        var hit = !q || o.label.toLowerCase().indexOf(q) >= 0;
        var aliasHit = "";
        if (!hit && q && o.aliases) {
          for (var i = 0; i < o.aliases.length; i++) {
            if (o.aliases[i].toLowerCase().indexOf(q) >= 0) { aliasHit = o.aliases[i]; hit = true; break; }
          }
        }
        if (!hit) { return; }
        shown.push(o.value);
        var item = document.createElement("div");
        item.className = "pk-item" + (selected.indexOf(o.value) >= 0 ? " is-on" : "");
        var nm = document.createElement("span");
        nm.className = "nm";
        nm.textContent = o.label;
        item.appendChild(nm);
        if (aliasHit) {
          var ax = document.createElement("span");
          ax.className = "ax";
          ax.textContent = "含「" + aliasHit + "」";
          item.appendChild(ax);
        } else if (q && o.aliases && o.aliases.length) {
          var hint = document.createElement("span");
          hint.className = "ax";
          hint.textContent = o.aliases.slice(0, 3).join(" ");
          item.appendChild(hint);
        }
        item.addEventListener("mousedown", function (ev) {
          ev.preventDefault();
          ev.stopPropagation();
          pick(o.value);
        });
        menu.appendChild(item);
      });
      active = -1;
      if (!shown.length) {
        var empty = document.createElement("div");
        empty.className = "pk-empty";
        empty.textContent = "没有匹配的选项";
        menu.appendChild(empty);
      }
    }

    function openMenu() {
      if (open) { renderMenu(); return; }
      open = true;
      field.classList.add("is-open");
      menu.hidden = false;
      renderMenu();
    }

    function closeMenu() {
      open = false;
      field.classList.remove("is-open");
      menu.hidden = true;
      input.value = "";
    }

    function pick(value) {
      var i = selected.indexOf(value);
      if (multi) {
        if (i >= 0) { selected.splice(i, 1); } else { selected.push(value); }
        renderTags();
        renderMenu();
        input.focus();
      } else {
        selected = (i >= 0) ? [] : [value];
        renderTags();
        closeMenu();
      }
      if (cfg.onChange) { cfg.onChange(selected.slice()); }
    }

    field.addEventListener("mousedown", function (ev) {
      if (ev.target === input) { return; }
      ev.preventDefault();
      input.focus();
      openMenu();
    });
    input.addEventListener("focus", openMenu);
    input.addEventListener("input", openMenu);
    input.addEventListener("keydown", function (ev) {
      if (ev.key === "Escape") { closeMenu(); input.blur(); return; }
      if (ev.key === "Backspace" && !input.value && selected.length) {
        selected.pop();
        renderTags();
        renderMenu();
        if (cfg.onChange) { cfg.onChange(selected.slice()); }
        return;
      }
      if (ev.key === "ArrowDown" || ev.key === "ArrowUp") {
        ev.preventDefault();
        if (!open) { openMenu(); return; }
        var items = menu.querySelectorAll(".pk-item");
        if (!items.length) { return; }
        active = ev.key === "ArrowDown"
          ? (active + 1) % items.length
          : (active <= 0 ? items.length - 1 : active - 1);
        for (var i = 0; i < items.length; i++) {
          items[i].classList.toggle("is-active", i === active);
        }
        items[active].scrollIntoView({ block: "nearest" });
        return;
      }
      if (ev.key === "Enter") {
        ev.preventDefault();
        if (active >= 0 && active < shown.length) { pick(shown[active]); }
        else if (shown.length === 1) { pick(shown[0]); }
        return;
      }
    });
    menu.addEventListener("mousedown", function (ev) { ev.preventDefault(); });

    document.addEventListener("mousedown", function (ev) {
      if (!host.contains(ev.target)) { closeMenu(); }
    });

    // 外部（全选 / 清空）改选中集合
    host.setValue = function (values) {
      selected = (values || []).slice();
      renderTags();
      if (open) { renderMenu(); }
      if (cfg.onChange) { cfg.onChange(selected.slice()); }
    };
    host.getValue = function () { return selected.slice(); };

    renderTags();
    return host;
  }

  function toOptions(values, aliases) {
    return values.map(function (v) {
      return { value: v, label: v, aliases: (aliases && aliases[v]) || [] };
    });
  }

  /* ---------------------------- 多选：查油价 ---------------------------- */
  var qtyEl = document.getElementById("calc-qty");
  var unitEl = document.getElementById("calc-unit");

  // 两个回调定义在 Picker 之前（函数声明提升）；真正被调用是在用户操作时，
  // 那时相关变量早已赋值。
  function persistReport() {
    saveState({
      report: {
        provinces: provincesPicker.getValue(),
        grades: gradesPicker.getValue()
      }
    });
  }
  function persistCalc() {
    saveState({
      calc: {
        province: calcProvince.getValue()[0] || "",
        grade: calcGrade.getValue()[0] || "",
        qty: qtyEl.value,
        unit: unitEl.value
      }
    });
  }

  var provincesPicker = Picker(document.getElementById("pk-provinces"), {
    multi: true,
    placeholder: "搜索省份或城市，可多选",
    options: toOptions(OPT.provinces || [], OPT.aliases || {}),
    value: pickList(SAVED_REPORT.provinces, OPT.provinces, OPT.defaultProvinces),
    onChange: persistReport
  });
  var gradesPicker = Picker(document.getElementById("pk-grades"), {
    multi: true,
    placeholder: "搜索油号，可多选",
    options: toOptions(OPT.grades || []),
    value: pickList(SAVED_REPORT.grades, OPT.grades, OPT.defaultGrades),
    onChange: persistReport
  });

  /* ---------------------------- 单选：算油费 ---------------------------- */
  var calcProvince = Picker(document.getElementById("pk-calc-province"), {
    multi: false,
    placeholder: "搜索省份或城市",
    options: toOptions(OPT.provinces || [], OPT.aliases || {}),
    value: pickOne(SAVED_CALC.province, OPT.provinces, (OPT.defaultProvinces || [])[0]),
    onChange: persistCalc
  });
  var calcGrade = Picker(document.getElementById("pk-calc-grade"), {
    multi: false,
    placeholder: "搜索油号",
    options: toOptions(OPT.grades || []),
    value: pickOne(SAVED_CALC.grade, OPT.grades, (OPT.defaultGrades || [])[0]),
    onChange: persistCalc
  });

  // 数量 / 单位也一并记住（只存用户填过的非空值，空着就保持页面默认的 30 升）
  if (typeof SAVED_CALC.qty === "string" && SAVED_CALC.qty) { qtyEl.value = SAVED_CALC.qty; }
  if (SAVED_CALC.unit === "元") { unitEl.value = "元"; }

  var saveTimer = 0;
  function persistCalcSoon() {   // 打字时防抖，不必每敲一个键都写一次
    clearTimeout(saveTimer);
    saveTimer = setTimeout(persistCalc, 300);
  }
  qtyEl.addEventListener("input", persistCalcSoon);
  unitEl.addEventListener("change", persistCalc);

  /* ------------------------------- 出图 -------------------------------- */
  function setMsg(id, text, kind) {
    var el = document.getElementById(id);
    el.className = "msg" + (text ? " is-" + (kind || "err") : "");
    el.textContent = text || "";
  }

  var live = {};   // 每种结果各记一个 blob URL，替换前释放，免得越积越多

  function showResult(kind, url, blob) {
    var img = document.getElementById("img-" + kind);
    var box = document.getElementById("res-" + kind);
    var link = document.getElementById("open-" + kind);
    if (live[kind]) { URL.revokeObjectURL(live[kind]); }
    live[kind] = URL.createObjectURL(blob);
    img.src = live[kind];
    link.href = url;
    box.classList.add("is-on");
  }

  function absUrl(url) { return new URL(url, location.href).href; }

  function wireCopy(btnId, linkId) {
    var btn = document.getElementById(btnId);
    var link = document.getElementById(linkId);
    btn.addEventListener("click", function () {
      var text = link.href;
      if (!text || text === "#") { return; }
      var done = function () {
        var old = btn.textContent;
        btn.textContent = "已复制 ✓";
        setTimeout(function () { btn.textContent = old; }, 1600);
      };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(done, function () {
          window.prompt("复制这个链接：", text);
        });
      } else {
        window.prompt("复制这个链接：", text);
      }
    });
  }
  wireCopy("copy-report", "open-report");
  wireCopy("copy-calc", "open-calc");

  async function run(kind, url, msgId, btn) {
    setMsg(msgId, "正在出图…", "info");
    btn.disabled = true;
    try {
      var resp = await fetch(url, { headers: { "Accept": "image/*,application/json" } });
      var ctype = resp.headers.get("content-type") || "";
      if (!resp.ok || ctype.indexOf("application/json") >= 0) {
        var text = await resp.text();
        var detail = text;
        try {
          var j = JSON.parse(text);
          if (j && j.error) { detail = j.error; }
        } catch (e) { /* 非 JSON 就原样显示 */ }
        throw new Error(detail || ("HTTP " + resp.status));
      }
      var blob = await resp.blob();
      if (!blob.size) { throw new Error("服务返回了空图片"); }
      showResult(kind, url, blob);
      setMsg(msgId, "");
    } catch (err) {
      setMsg(msgId, "出图失败：" + (err && err.message ? err.message : err));
    } finally {
      btn.disabled = false;
    }
  }

  var btnReport = document.getElementById("btn-report");
  btnReport.addEventListener("click", function () {
    var provinces = provincesPicker.getValue();
    var grades = gradesPicker.getValue();
    if (!provinces.length) { setMsg("msg-report", "请至少选一个地区。"); return; }
    if (!grades.length) { setMsg("msg-report", "请至少选一个油号。"); return; }
    run("report", oilUrl({
      type: "report",
      provinces: provinces.join(","),
      grades: grades.join(",")
    }), "msg-report", btnReport);
  });

  var btnCalc = document.getElementById("btn-calc");
  btnCalc.addEventListener("click", function () {
    var province = calcProvince.getValue()[0];
    var grade = calcGrade.getValue()[0];
    var qtyEl = document.getElementById("calc-qty");
    var unit = document.getElementById("calc-unit").value;
    var qty = String(qtyEl.value || "").trim();
    if (!province) { setMsg("msg-calc", "请选一个地区。"); return; }
    if (!grade) { setMsg("msg-calc", "请选一个油号。"); return; }
    if (!(parseFloat(qty) > 0)) { setMsg("msg-calc", "数量要填一个大于 0 的数字。"); return; }
    run("calc", oilUrl({
      type: "calc",
      province: province,
      grade: grade,
      amount: qty + unit
    }), "msg-calc", btnCalc);
  });

  /* ------------------------------- 切换标签 ----------------------------- */
  var tabs = [
    { tab: document.getElementById("tab-report"), panel: document.getElementById("panel-report"), name: "report" },
    { tab: document.getElementById("tab-calc"), panel: document.getElementById("panel-calc"), name: "calc" }
  ];
  // remember=false 专供「打开页面时回显」用：仅仅打开一次不该产生一次写入。
  function activateTab(idx, remember) {
    tabs.forEach(function (pair, j) {
      var on = idx === j;
      pair.tab.setAttribute("aria-selected", on ? "true" : "false");
      pair.panel.hidden = !on;
    });
    if (remember !== false && tabs[idx]) { saveState({ tab: tabs[idx].name }); }
  }
  tabs.forEach(function (pair, idx) {
    pair.tab.addEventListener("click", function () { activateTab(idx); });
  });

  // 回显上次停留的标签；存的值不认识（或从没存过）就停在默认的「查油价」（idx 0）。
  var savedTabIdx = -1;
  for (var ti = 0; ti < tabs.length; ti++) {
    if (tabs[ti].name === SAVED.tab) { savedTabIdx = ti; break; }
  }
  if (savedTabIdx > 0) { activateTab(savedTabIdx, false); }
})();
</script>
</body>
</html>
"""
