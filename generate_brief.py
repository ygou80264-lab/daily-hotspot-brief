#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日热点简报自动化清洗与静态页生成脚本（零第三方依赖）
支持：链接清洗、重复度质检、配额兜底、Top 3 速览、深色模式、localStorage 已读追踪、一键复制、前端实时热更新
"""

import argparse
import json
import re
import shutil
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "spm", "from", "isappinstalled", "share_token", "vd_source",
}

CANONICAL_SECTIONS = {
    "domestic": "国内时政/社会",
    "world": "国际动态",
    "tech": "科技/互联网",
    "ai": "AI与政策",
    "business": "财经/商业",
}

PWA_ICON_SIZES = (192, 512)

PWA_MANIFEST = {
    "name": "每日热点简报",
    "short_name": "每日热点",
    "description": "每天自动更新的五类热点简报",
    "lang": "zh-CN",
    "id": "./",
    "start_url": "./",
    "scope": "./",
    "display": "standalone",
    "background_color": "#f8f9fa",
    "theme_color": "#2563eb",
    "icons": [
        {
            "src": f"./icon-{size}.png",
            "sizes": f"{size}x{size}",
            "type": "image/png",
            "purpose": purpose,
        }
        for size in PWA_ICON_SIZES
        for purpose in ("any", "maskable")
    ],
}

SERVICE_WORKER = """const CACHE_NAME = "daily-brief-v3";
const SHELL = ["./", "./index.html", "./manifest.webmanifest", "./icon-192.png", "./icon-512.png"];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE_NAME).then((cache) => cache.addAll(SHELL)));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  if (event.request.method !== "GET") return;
  const url = new URL(event.request.url);
  if (url.origin !== self.location.origin) return;

  event.respondWith(
    fetch(event.request)
      .then((response) => {
        const copy = response.clone();
        caches.open(CACHE_NAME).then((cache) => cache.put(event.request, copy));
        return response;
      })
      .catch(async () => {
        return (await caches.match(event.request)) || (await caches.match("./index.html"));
      })
  );
});
"""


def write_pwa_assets(output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "manifest.webmanifest").write_text(
        json.dumps(PWA_MANIFEST, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (output_dir / "sw.js").write_text(SERVICE_WORKER, encoding="utf-8")

    asset_dir = Path(__file__).resolve().parent / "assets"
    for size in PWA_ICON_SIZES:
        source = asset_dir / f"icon-{size}.png"
        if source.exists():
            shutil.copy2(source, output_dir / source.name)


def clean_url(url: str, verify_https: bool = False) -> str:
    if not url:
        return ""
    url = url.strip()
    url = re.sub(r"^https?://[^/]*?big5[^/]*/gate/big5/", "https://", url, flags=re.IGNORECASE)
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    parsed = urlparse(url)
    clean_query = [
        (k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if k.lower() not in TRACKING_PARAMS
    ]
    url = urlunparse(parsed._replace(query=urlencode(clean_query)))

    if url.startswith("http://"):
        https_url = "https://" + url[len("http://"):]
        if not verify_https:
            return https_url
        try:
            req = urllib.request.Request(https_url, method="HEAD", headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=3) as resp:
                if resp.status < 400:
                    return https_url
        except (urllib.error.URLError, TimeoutError, ValueError):
            pass
    return url


def check_title_summary_overlap(title: str, summary: str) -> dict:
    t = re.sub(r"[^\w\u4e00-\u9fff]", "", title)
    s = re.sub(r"[^\w\u4e00-\u9fff]", "", summary)
    prefix_repeat = len(t) >= 6 and s.startswith(t[:6])
    t_chars = set(t)
    overlap_ratio = len(t_chars & set(s)) / max(len(t_chars), 1)
    return {
        "is_redundant": prefix_repeat or overlap_ratio > 0.72,
        "prefix_repeat": prefix_repeat,
        "overlap_ratio": round(overlap_ratio, 2),
    }


def build_focus_items(data: dict, limit: int = 5) -> list[dict]:
    sections = data.get("sections", [])
    all_items = [
        item
        for section in sections
        for item in section.get("items", [])
    ]
    by_id = {item.get("id"): item for item in all_items if item.get("id")}
    selected = []
    selected_ids = set()

    for top_item in data.get("top_3", []):
        item_id = top_item.get("ref_id")
        if item_id in by_id and item_id not in selected_ids:
            selected.append(by_id[item_id])
            selected_ids.add(item_id)

    for item in all_items:
        if len(selected) >= limit:
            break
        if item.get("importance") == "high" and item.get("id") not in selected_ids:
            selected.append(item)
            selected_ids.add(item.get("id"))

    for item in all_items:
        if len(selected) >= limit:
            break
        if item.get("id") not in selected_ids:
            selected.append(item)
            selected_ids.add(item.get("id"))

    return selected[:limit]


def clean_and_validate(data: dict, verify_https: bool = False) -> tuple[dict, list[str]]:
    warnings = []
    top3_ids = {item.get("ref_id") for item in data.get("top_3", []) if item.get("ref_id")}
    all_items = []

    for sec in data.get("sections", []):
        sec_id = sec.get("id", "").strip()
        if sec_id in CANONICAL_SECTIONS:
            sec["name"] = CANONICAL_SECTIONS[sec_id]

        for idx, item in enumerate(sec.get("items", []), start=1):
            item["id"] = item.get("id") or f"{sec_id}-{idx:02d}"
            item["title"] = item.get("title", "").strip()
            item["summary"] = item.get("summary", "").strip()
            item["impact"] = item.get("impact", "").strip()
            item["source_name"] = item.get("source_name", "原文").strip()
            item["url"] = clean_url(item.get("url", ""), verify_https=verify_https)
            item["importance"] = "high" if item.get("importance") == "high" else "normal"

            qa = check_title_summary_overlap(item["title"], item["summary"])
            if qa["is_redundant"]:
                warnings.append(
                    f"[摘要重复告警] {item['id']}《{item['title']}》"
                    f"(前缀重复={qa['prefix_repeat']}, 重合率={qa['overlap_ratio']})"
                )
            all_items.append(item)

    high_items = [it for it in all_items if it["importance"] == "high"]
    if len(high_items) > 5:
        kept = 0
        for it in sorted(high_items, key=lambda x: x["id"] not in top3_ids):
            if kept < 5:
                kept += 1
            else:
                it["importance"] = "normal"
        warnings.append("[配额修正] high 条目超过 5 条，已自动降级多余条目。")
    elif len(high_items) == 0 and top3_ids:
        for it in all_items:
            if it["id"] in top3_ids:
                it["importance"] = "high"
        warnings.append("[配额修正] 原数据无 high 条目，已自动提权 top_3 对应条目。")

    tz_cn = timezone(timedelta(hours=8))
    now_cn = datetime.now(tz_cn)
    data.setdefault("date", now_cn.strftime("%Y-%m-%d"))
    data["updated_at"] = now_cn.strftime("%H:%M")
    data["version"] = int(time.time())
    data["focus_items"] = build_focus_items(data)
    return data, warnings


def render_html(data: dict, json_url: str = "./latest.json") -> str:
    initial_json = json.dumps(data, ensure_ascii=False).replace("</script>", "<\\/script>")
    json_url_js = json.dumps(json_url, ensure_ascii=False)

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover" />
  <meta name="theme-color" content="#2563eb" />
  <meta name="apple-mobile-web-app-capable" content="yes" />
  <meta name="apple-mobile-web-app-title" content="每日简报" />
  <link rel="manifest" href="./manifest.webmanifest" />
  <link rel="apple-touch-icon" href="./icon-192.png" />
  <title>每日热点简报</title>
  <style>
    :root {{
      --bg: #f8f9fa; --card: #ffffff; --text: #1a1d21; --muted: #5c6672;
      --border: #e6e8ec; --accent: #2563eb; --high-bg: #f0f5ff;
      --high-border: #2563eb; --badge-bg: #dbeafe; --badge-text: #1d4ed8;
      --nav-bg: rgba(248, 249, 250, 0.88); --ok: #16a34a;
    }}
    @media (prefers-color-scheme: dark) {{
      :root:not([data-theme="light"]) {{
        --bg: #0f1115; --card: #171a21; --text: #e6e8ec; --muted: #9aa3af;
        --border: #272c36; --accent: #60a5fa; --high-bg: #172033;
        --high-border: #3b82f6; --badge-bg: #1e3a5f; --badge-text: #93c5fd;
        --nav-bg: rgba(15, 17, 21, 0.88); --ok: #4ade80;
      }}
    }}
    :root[data-theme="dark"] {{
      --bg: #0f1115; --card: #171a21; --text: #e6e8ec; --muted: #9aa3af;
      --border: #272c36; --accent: #60a5fa; --high-bg: #172033;
      --high-border: #3b82f6; --badge-bg: #1e3a5f; --badge-text: #93c5fd;
      --nav-bg: rgba(15, 17, 21, 0.88); --ok: #4ade80;
    }}
    * {{ box-sizing: border-box; }}
    html {{ scroll-behavior: smooth; }}
    body {{
      margin: 0; background: var(--bg); color: var(--text);
      font: 15px/1.65 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "PingFang SC", "Microsoft YaHei", sans-serif;
    }}
    .wrap {{ max-width: 720px; margin: 0 auto; padding: 20px 16px 64px; }}
    header {{ margin-bottom: 18px; }}
    .eyebrow {{
      display: flex; justify-content: space-between; align-items: center; gap: 8px; flex-wrap: wrap;
      font-size: 12px; letter-spacing: 0.04em; color: var(--muted);
    }}
    .live-dot {{
      display: inline-block; width: 7px; height: 7px; border-radius: 50%;
      background: var(--ok); margin-right: 5px; vertical-align: 1px;
    }}
    .toolbar {{ display: flex; gap: 6px; }}
    .tool-btn {{
      background: var(--card); color: var(--text); border: 1px solid var(--border);
      border-radius: 6px; padding: 4px 9px; font-size: 12px; cursor: pointer;
    }}
    .tool-btn:hover {{ border-color: var(--accent); color: var(--accent); }}
    h1 {{ margin: 8px 0 6px; font-size: 25px; line-height: 1.25; }}
    .meta {{ margin: 0; font-size: 13px; color: var(--muted); display: flex; gap: 12px; flex-wrap: wrap; }}
    .install-btn {{
      margin-top: 10px; padding: 7px 12px; border: 1px solid var(--accent);
      border-radius: 7px; background: var(--card); color: var(--accent);
      font-size: 13px; font-weight: 700; cursor: pointer;
    }}
    .install-hint {{
      margin: 7px 0 0; padding: 9px 11px; border-radius: 8px;
      background: var(--high-bg); color: var(--muted); font-size: 12px; line-height: 1.6;
    }}
    .focus-card {{
      background: var(--high-bg); border: 1px solid var(--border);
      border-left: 4px solid var(--high-border); border-radius: 10px;
      padding: 14px 16px; margin-bottom: 18px;
    }}
    .focus-label {{ font-size: 12px; font-weight: 700; color: var(--accent); margin-bottom: 10px; }}
    .focus-list {{ display: grid; gap: 10px; }}
    .focus-item {{ padding-top: 10px; border-top: 1px solid var(--border); }}
    .focus-item:first-child {{ padding-top: 0; border-top: 0; }}
    .focus-rank {{ color: var(--accent); font-size: 11px; font-weight: 750; }}
    .focus-item h3 {{ margin: 3px 0 0; font-size: 15px; line-height: 1.45; }}
    .focus-item p {{ margin: 4px 0 0; color: var(--muted); font-size: 13px; }}
    .focus-item .src-link {{ display: inline-block; margin-top: 5px; }}
    .industry-card {{
      margin-bottom: 18px; padding: 16px; border-radius: 12px;
      background: #123a42; color: #fff;
    }}
    .industry-card h2 {{ margin: 3px 0 6px; color: #fff; font-size: 22px; }}
    .industry-kicker {{ margin: 0; color: #9ed3d2; font-size: 12px; font-weight: 750; }}
    .industry-trigger {{ margin: 0 0 12px; color: rgba(255,255,255,.75); font-size: 13px; }}
    .industry-flow {{ display: grid; gap: 8px; }}
    .industry-stage {{
      padding: 10px; border: 1px solid rgba(255,255,255,.16);
      border-radius: 10px; background: rgba(255,255,255,.07);
    }}
    .industry-stage h3 {{ margin: 0 0 7px; color: #9ed3d2; font-size: 13px; }}
    .industry-node {{
      margin-top: 6px; border-radius: 8px; background: rgba(255,255,255,.1);
    }}
    .industry-node summary {{
      padding: 8px 9px; cursor: pointer; font-size: 13px; font-weight: 650;
      list-style-position: inside;
    }}
    .industry-node p {{
      margin: 0; padding: 0 9px 8px; color: rgba(255,255,255,.82);
      font-size: 12.5px; line-height: 1.6;
    }}
    .industry-analysis, .industry-concepts, .industry-question {{
      margin-top: 10px; padding: 12px; border-radius: 10px;
      background: rgba(255,255,255,.95); color: var(--text);
    }}
    .industry-analysis h3, .industry-concepts h3, .industry-question h3 {{
      margin: 0 0 6px; color: var(--accent); font-size: 14px;
    }}
    .industry-analysis p, .industry-concepts p, .industry-question p {{
      margin: 4px 0; color: var(--muted); font-size: 13px; line-height: 1.65;
    }}
    .industry-sources {{ margin: 8px 0 0; font-size: 12px; }}
    .industry-sources a {{ color: #bfe9e6; margin-right: 10px; }}
    .full-brief {{ margin: 18px 0 0; }}
    .full-brief > summary {{
      cursor: pointer; padding: 12px 14px; border: 1px solid var(--border);
      border-radius: 9px; background: var(--card); color: var(--accent);
      font-size: 14px; font-weight: 700; list-style-position: inside;
    }}
    .full-brief[open] > summary {{ margin-bottom: 16px; }}
    .full-count {{ float: right; color: var(--muted); font-weight: 500; }}
    nav.cat-nav {{
      position: sticky; top: 0; z-index: 10; background: var(--nav-bg);
      backdrop-filter: blur(8px); padding: 10px 0; margin: 0 -4px 18px;
      display: flex; gap: 8px; overflow-x: auto; border-bottom: 1px solid var(--border);
    }}
    nav.cat-nav a {{
      white-space: nowrap; font-size: 13px; color: var(--muted); text-decoration: none;
      padding: 4px 10px; border-radius: 99px; background: var(--card); border: 1px solid var(--border);
    }}
    nav.cat-nav a:hover {{ color: var(--accent); border-color: var(--accent); }}
    .news-section {{ margin-bottom: 28px; scroll-margin-top: 64px; }}
    .sec-head {{
      display: flex; justify-content: space-between; align-items: baseline;
      margin-bottom: 10px; padding-bottom: 6px; border-bottom: 1px solid var(--border);
    }}
    .sec-head h2 {{ margin: 0; font-size: 17px; }}
    .sec-count {{ font-size: 12px; color: var(--muted); }}
    .news-card {{
      background: var(--card); border: 1px solid var(--border); border-radius: 10px;
      padding: 14px 16px; margin-bottom: 10px; transition: opacity 0.2s; scroll-margin-top: 68px;
    }}
    .news-card.is-high {{ border-left: 3px solid var(--high-border); }}
    .news-card.is-read {{ opacity: 0.52; }}
    .card-head {{ display: flex; align-items: flex-start; gap: 8px; }}
    .idx {{ font-size: 12px; font-weight: 700; color: var(--muted); padding-top: 3px; font-variant-numeric: tabular-nums; }}
    .card-head h3 {{ margin: 0; font-size: 15.5px; line-height: 1.4; flex: 1; }}
    .badge-high {{
      display: inline-block; margin-left: 6px; padding: 1px 6px; font-size: 11px;
      border-radius: 4px; background: var(--badge-bg); color: var(--badge-text); vertical-align: 1px;
    }}
    .read-btn {{
      background: transparent; border: 1px solid var(--border); color: var(--muted);
      border-radius: 4px; font-size: 11px; padding: 2px 6px; cursor: pointer; flex-shrink: 0;
    }}
    .news-card.is-read .read-btn {{ border-color: var(--accent); color: var(--accent); }}
    .summary {{ margin: 6px 0 0 24px; color: var(--muted); font-size: 14px; }}
    .impact {{
      margin: 6px 0 0 24px; font-size: 13px; color: var(--text);
      background: var(--high-bg); padding: 4px 8px; border-radius: 6px;
    }}
    .card-foot {{ margin: 8px 0 0 24px; }}
    .src-link {{ font-size: 12.5px; color: var(--accent); text-decoration: none; }}
    .src-link:hover {{ text-decoration: underline; }}
    footer {{
      margin-top: 36px; padding-top: 16px; border-top: 1px solid var(--border);
      font-size: 12.5px; color: var(--muted); text-align: center;
    }}
    footer a {{ color: var(--accent); text-decoration: none; }}
  </style>
</head>
<body id="top">
  <div class="wrap">
    <header>
      <div class="eyebrow">
        <span id="liveStatus"><span class="live-dot"></span>更新于 --:--</span>
        <div class="toolbar">
          <button type="button" class="tool-btn" id="refreshBtn">刷新</button>
          <button type="button" class="tool-btn" id="copyBtn">复制</button>
          <button type="button" class="tool-btn" id="themeBtn">主题</button>
        </div>
      </div>
      <h1 id="pageTitle">每日热点简报</h1>
      <p class="meta">
        <span id="metaCount">加载中...</span>
        <span id="progressText">已读 0/0</span>
      </p>
      <button type="button" class="install-btn" id="installBtn">安装到桌面</button>
      <p class="install-hint" id="installHint" hidden></p>
    </header>

    <div id="industryMount"></div>
    <div id="focusMount"></div>
    <details class="full-brief" id="fullBrief">
      <summary>展开全部新闻 <span class="full-count" id="fullCount"></span></summary>
      <nav class="cat-nav" id="navMount" aria-label="分类导航"></nav>
      <main id="mainMount"></main>
    </details>

    <footer>
      来源链接均已清洗核验 · 每 5 分钟自动检查更新 · <a href="#top">返回顶部 ↑</a>
    </footer>
  </div>

  <script>
    (function () {{
      const DATA_URL = {json_url_js};
      const POLL_MS = 5 * 60 * 1000;
      let currentData = {initial_json};
      let readSet = new Set();
      let deferredInstallPrompt = null;

      const installBtn = document.getElementById("installBtn");
      const installHint = document.getElementById("installHint");
      if (window.matchMedia("(display-mode: standalone)").matches) {{
        installBtn.hidden = true;
      }}
      window.addEventListener("beforeinstallprompt", (event) => {{
        event.preventDefault();
        deferredInstallPrompt = event;
        installBtn.textContent = "安装应用";
      }});
      installBtn.addEventListener("click", async () => {{
        if (deferredInstallPrompt) {{
          deferredInstallPrompt.prompt();
          await deferredInstallPrompt.userChoice;
          deferredInstallPrompt = null;
          return;
        }}
        const ua = navigator.userAgent;
        installHint.hidden = false;
        if (/MicroMessenger/i.test(ua)) {{
          installHint.textContent = "微信内置浏览器不能安装。请点右上角“…” → 在浏览器打开，再用 Chrome 安装。";
        }} else if (/Chrome/i.test(ua)) {{
          installHint.textContent = "请刷新页面并等待几秒，再点 Chrome 右上角“⋮” → 安装应用或添加到主屏幕。";
        }} else {{
          installHint.textContent = "请复制本页网址到 Chrome 打开，然后点击右上角菜单添加到主屏幕。";
        }}
      }});

      const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({{
        "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
      }}[c]));

      function getStorageKey(date) {{ return "brief_read_" + date; }}
      function loadReadSet(date) {{
        readSet = new Set(JSON.parse(localStorage.getItem(getStorageKey(date)) || "[]"));
      }}

      function syncReadUI() {{
        const cards = document.querySelectorAll(".news-card");
        cards.forEach((card) => {{
          const id = card.dataset.id;
          const isRead = readSet.has(id);
          card.classList.toggle("is-read", isRead);
          const btn = card.querySelector(".read-btn");
          if (btn) btn.textContent = isRead ? "已读" : "未读";
        }});
        document.getElementById("progressText").textContent = `已读 ${{readSet.size}}/${{cards.length}}`;
        localStorage.setItem(getStorageKey(currentData.date), JSON.stringify([...readSet]));
      }}

      function renderIndustry(chain) {{
        if (!chain || !Array.isArray(chain.stages) || !chain.stages.length) return "";
        const stages = chain.stages.map((stage) => `
          <section class="industry-stage">
            <h3>${{esc(stage.layer)}}</h3>
            ${{(stage.items || []).map((item) => `
              <details class="industry-node">
                <summary>产业链节点解释 · ${{esc(item.name)}} · 点击展开</summary>
                <p><b>它做什么：</b>${{esc(item.role)}}</p>
                <p><b>说白了：</b>${{esc(item.plain)}}</p>
                <p><b>典型角色：</b>${{esc((item.examples || []).join("、"))}}</p>
                <p><b>可能卡点：</b>${{esc(item.bottleneck)}}</p>
                <p><b>议价权：</b>${{esc(item.power)}}</p>
              </details>
            `).join("")}}
          </section>
        `).join("");
        const sources = (chain.sources || []).map((source) =>
          `<a href="${{esc(source.url)}}" target="_blank" rel="noopener noreferrer">${{esc(source.name)}}原文 ↗</a>`
        ).join("");
        const concepts = (chain.concepts || []).map((concept) =>
          `<p><b>${{esc(concept.term)}}</b>：${{esc(concept.plain)}}</p>`
        ).join("");

        return `
          <section class="industry-card" aria-label="每日产业链分析">
            <p class="industry-kicker">每日产业链练习 · 今天换一条链</p>
            <h2>${{esc(chain.title)}}</h2>
            <p class="industry-trigger">${{esc(chain.trigger)}}</p>
            <div class="industry-sources">${{sources}}</div>
            <div class="industry-flow">${{stages}}</div>
            <div class="industry-analysis">
              <h3>变化点</h3><p>${{esc(chain.change)}}</p>
              <h3>透过现象看本质</h3><p>${{esc(chain.essence)}}</p>
              <h3>什么会推翻判断</h3><p>${{esc(chain.counter_evidence)}}</p>
            </div>
            <div class="industry-concepts"><h3>概念与白话</h3>${{concepts}}</div>
            <div class="industry-question"><h3>轮到你判断</h3><p>${{esc(chain.question)}}</p></div>
          </section>`;
      }}

      function renderBrief(data) {{
        currentData = data;
        loadReadSet(data.date);
        const sections = data.sections || [];
        const total = sections.reduce((sum, s) => sum + (s.items || []).length, 0);

        document.title = `${{data.date}} 每日热点简报`;
        document.getElementById("pageTitle").textContent = `${{data.date}} 每日热点简报`;
        document.getElementById("liveStatus").innerHTML =
          `<span class="live-dot"></span>更新于 ${{esc(data.updated_at || "--:--")}}`;
        const allItems = sections.flatMap((s) => s.items || []);
        const focusItems = data.focus_items?.length ? data.focus_items : allItems.slice(0, 5);
        document.getElementById("metaCount").textContent =
          `今天先看 ${{focusItems.length}} 条 · 共 ${{total}} 条`;
        document.getElementById("fullCount").textContent = `${{total}} 条`;
        document.getElementById("industryMount").innerHTML = renderIndustry(data.industry_chain);

        document.getElementById("focusMount").innerHTML = focusItems.length ? `
          <section class="focus-card" aria-label="今日优先阅读">
            <div class="focus-label">先看这 ${{focusItems.length}} 条 · 其余新闻已折叠</div>
            <div class="focus-list">
              ${{focusItems.map((it, i) => `
                <article class="focus-item">
                  <span class="focus-rank">0${{i + 1}} · ${{i < 3 ? "重点" : "跟进"}}</span>
                  <h3>${{esc(it.title)}}</h3>
                  <p>${{esc(it.summary)}}</p>
                  ${{it.impact ? `<p><strong>看点：</strong>${{esc(it.impact)}}</p>` : ""}}
                  <a class="src-link" href="${{esc(it.url)}}" rel="noopener noreferrer"
                     target="_blank">查看${{esc(it.source_name)}}原文 ↗</a>
                </article>
              `).join("")}}
            </div>
          </section>` : "";

        document.getElementById("navMount").innerHTML = sections
          .map((s) => `<a href="#sec-${{esc(s.id)}}">${{esc(s.name)}}</a>`)
          .join("");

        document.getElementById("mainMount").innerHTML = sections.map((s) => `
          <section class="news-section" id="sec-${{esc(s.id)}}">
            <div class="sec-head">
              <h2>${{esc(s.name)}}</h2>
              <span class="sec-count">${{(s.items || []).length}} 条</span>
            </div>
            ${{(s.items || []).map((it, idx) => {{
              const isHigh = it.importance === "high";
              return `
                <article class="news-card ${{isHigh ? "is-high" : ""}}" id="${{esc(it.id)}}" data-id="${{esc(it.id)}}">
                  <div class="card-head">
                    <span class="idx">${{String(idx + 1).padStart(2, "0")}}</span>
                    <h3>${{esc(it.title)}}${{isHigh ? '<span class="badge-high">焦点</span>' : ""}}</h3>
                    <button type="button" class="read-btn" data-toggle-read="${{esc(it.id)}}">未读</button>
                  </div>
                  <p class="summary">${{esc(it.summary)}}</p>
                  ${{it.impact ? `<p class="impact"><strong>看点：</strong>${{esc(it.impact)}}</p>` : ""}}
                  <div class="card-foot">
                    <a class="src-link" href="${{esc(it.url)}}" data-mark-read="${{esc(it.id)}}" rel="noopener noreferrer" target="_blank">
                      查看${{esc(it.source_name)}}原文 ↗
                    </a>
                  </div>
                </article>`;
            }}).join("")}}
          </section>
        `).join("");

        syncReadUI();
      }}

      function buildPlainText(data) {{
        const lines = [`# ${{data.date}} 每日热点简报（更新于 ${{data.updated_at}}）`, ""];
        if ((data.top_3 || []).length) {{
          lines.push("## 今日 Top 3");
          data.top_3.forEach((t, i) => lines.push(`${{i + 1}}. ${{t.headline}}`));
          lines.push("");
        }}
        (data.sections || []).forEach((s) => {{
          lines.push(`## ${{s.name}}`);
          (s.items || []).forEach((it, i) => {{
            const tag = it.importance === "high" ? "【焦点】" : "";
            lines.push(`${{String(i + 1).padStart(2, "0")}}. ${{tag}}${{it.title}}`);
            lines.push(`    ${{it.summary}}`);
            if (it.impact) lines.push(`    看点：${{it.impact}}`);
            lines.push(`    来源：${{it.source_name}} (${{it.url}})`);
          }});
          lines.push("");
        }});
        return lines.join("\\n").trim();
      }}

      const refreshBtn = document.getElementById("refreshBtn");
      async function checkUpdate(manual = false) {{
        if (location.protocol === "file:") return;
        if (manual) refreshBtn.textContent = "检查中...";
        try {{
          const res = await fetch(`${{DATA_URL}}?t=${{Date.now()}}`, {{ cache: "no-store" }});
          if (!res.ok) throw new Error("HTTP " + res.status);
          const fresh = await res.json();
          const changed = fresh.version !== currentData.version || fresh.updated_at !== currentData.updated_at;
          if (changed) {{
            renderBrief(fresh);
            if (manual) refreshBtn.textContent = "已更新 ✓";
          }} else if (manual) {{
            refreshBtn.textContent = "已是最新";
          }}
        }} catch {{
          if (manual) refreshBtn.textContent = "失败";
        }} finally {{
          if (manual) setTimeout(() => (refreshBtn.textContent = "刷新"), 1600);
        }}
      }}

      document.addEventListener("click", (e) => {{
        const toggleBtn = e.target.closest("[data-toggle-read]");
        if (toggleBtn) {{
          const id = toggleBtn.dataset.toggleRead;
          readSet.has(id) ? readSet.delete(id) : readSet.add(id);
          syncReadUI();
          return;
        }}
        const link = e.target.closest("[data-mark-read]");
        if (link) {{
          readSet.add(link.dataset.markRead);
          syncReadUI();
        }}
      }});

      refreshBtn.addEventListener("click", () => checkUpdate(true));
      document.addEventListener("visibilitychange", () => {{
        if (document.visibilityState === "visible") checkUpdate(false);
      }});
      setInterval(() => checkUpdate(false), POLL_MS);

      const root = document.documentElement;
      const savedTheme = localStorage.getItem("brief_theme");
      if (savedTheme) root.setAttribute("data-theme", savedTheme);
      document.getElementById("themeBtn").addEventListener("click", () => {{
        const isDark = root.getAttribute("data-theme") === "dark" ||
          (!root.getAttribute("data-theme") && window.matchMedia("(prefers-color-scheme: dark)").matches);
        const next = isDark ? "light" : "dark";
        root.setAttribute("data-theme", next);
        localStorage.setItem("brief_theme", next);
      }});

      const copyBtn = document.getElementById("copyBtn");
      copyBtn.addEventListener("click", async () => {{
        try {{
          await navigator.clipboard.writeText(buildPlainText(currentData));
          copyBtn.textContent = "已复制 ✓";
          setTimeout(() => (copyBtn.textContent = "复制"), 1600);
        }} catch {{
          alert("复制失败，请检查剪贴板权限");
        }}
      }});

      if ("serviceWorker" in navigator) {{
        window.addEventListener("load", () => {{
          navigator.serviceWorker.register("./sw.js").catch(() => {{}});
        }});
      }}

      renderBrief(currentData);
      checkUpdate(false);
    }})();
  </script>
</body>
</html>"""


SAMPLE_DATA = {
    "date": "2026-11-21",
    "top_3": [
        {"ref_id": "world-01", "headline": "示例：配置 DEEPSEEK_API_KEY 后将自动抓取每日真实热点"},
    ],
    "industry_chain": {
        "title": "AI芯片与算力产业链",
        "trigger": "示例：用AI模型调用增长和免费额度收紧，练习判断算力供给与需求。",
        "sources": [{"name": "GitHub", "url": "https://github.com"}],
        "stages": [
            {
                "layer": "上游",
                "items": [
                    {
                        "name": "芯片与设备",
                        "role": "提供算力芯片和制造设备",
                        "plain": "这是AI运行需要的发动机和生产线。",
                        "examples": ["芯片设计公司", "半导体设备公司"],
                        "bottleneck": "先进芯片与设备供给",
                        "power": "越难替代，议价权越强",
                    }
                ],
            },
            {
                "layer": "中游",
                "items": [
                    {
                        "name": "云平台与模型推理",
                        "role": "把芯片变成可调用的模型服务",
                        "plain": "企业按使用量租算力、调用模型。",
                        "examples": ["云服务商", "模型API平台"],
                        "bottleneck": "推理成本和稳定性",
                        "power": "规模与客户入口带来优势",
                    }
                ],
            },
            {
                "layer": "下游",
                "items": [
                    {
                        "name": "AI应用",
                        "role": "把模型能力装入办公、编程和行业工具",
                        "plain": "用户看到的是能完成任务的软件。",
                        "examples": ["办公工具", "编程工具"],
                        "bottleneck": "可靠性和付费意愿",
                        "power": "掌握用户入口与行业数据更重要",
                    }
                ],
            },
            {
                "layer": "最终用户",
                "items": [
                    {
                        "name": "个人与企业",
                        "role": "使用AI服务并决定是否付费",
                        "plain": "需求大小最终决定上游投入能否收回。",
                        "examples": ["学生", "企业客户"],
                        "bottleneck": "信任、价格和实际效果",
                        "power": "用户可以切换或停止使用",
                    }
                ],
            },
        ],
        "change": "模型调用增加会推高算力需求，但仅凭限流不能断定瓶颈一定在芯片。",
        "essence": "需求增长不代表每一环都赚钱，价值通常流向更稀缺、更难替代的环节。",
        "counter_evidence": "若供应充足、服务快速降价且平台利润稳定，供给瓶颈判断就不成立。",
        "question": "如果AI用户翻倍，你认为最先紧张的是芯片、电力、云平台还是应用公司现金？",
        "concepts": [
            {"term": "上游议价权", "plain": "少数公司掌握关键供给时，更容易涨价。"},
            {"term": "算力成本", "plain": "AI回答越多、越长，消耗的芯片、电力与时间越多。"},
        ],
    },
    "sections": [
        {
            "id": "world",
            "name": "国际动态",
            "items": [
                {
                    "id": "world-01",
                    "importance": "high",
                    "title": "系统已就绪（当前为内置示例）",
                    "summary": "在 GitHub 仓库 Settings -> Secrets 中添加 DEEPSEEK_API_KEY 后，每天早8点将自动抓取并更新。",
                    "impact": "支持手机端添加到主屏幕、深色模式与已读进度保存。",
                    "source_name": "GitHub",
                    "url": "https://github.com",
                }
            ],
        }
    ],
}


def main():
    parser = argparse.ArgumentParser(description="清洗简报 JSON 并生成支持实时热更新的静态页")
    parser.add_argument("-i", "--input", help="输入的 JSON 文件路径（不传则使用内置示例）")
    parser.add_argument("-o", "--output", default="index.html", help="输出的 HTML 文件路径")
    parser.add_argument("--json-out", default="latest.json", help="供前端实时拉取的 JSON 路径")
    parser.add_argument("--verify-https", action="store_true", help="联网探活验证 http 升级 https")
    args = parser.parse_args()

    raw_data = json.loads(Path(args.input).read_text(encoding="utf-8")) if args.input else SAMPLE_DATA
    cleaned, warnings = clean_and_validate(raw_data, verify_https=args.verify_https)

    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
    write_pwa_assets(Path(args.output).parent)
    Path(args.json_out).write_text(json.dumps(cleaned, ensure_ascii=False, indent=2), encoding="utf-8")
    Path(args.output).write_text(render_html(cleaned, json_url=f"./{Path(args.json_out).name}"), encoding="utf-8")

    print(f"[{cleaned['updated_at']}] 已生成 {args.output} & {args.json_out}")
    for w in warnings:
        print("  ", w)


if __name__ == "__main__":
    main()
