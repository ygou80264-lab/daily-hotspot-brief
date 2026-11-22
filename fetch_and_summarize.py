#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
全自动新闻抓取与 DeepSeek 总结脚本（零第三方依赖）
功能：抓取国内外免费 RSS 新闻源 -> 调用 DeepSeek API 按极简情报规范生成 brief.json
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

# 免费、稳定、免翻墙/海外服务器(GitHub Actions)均可直连的 RSS 源
RSS_FEEDS = {
    "domestic": [
        ("中国新闻网-时政", "https://www.chinanews.com.cn/rss/china.xml"),
        ("中国新闻网-社会", "https://www.chinanews.com.cn/rss/society.xml"),
    ],
    "world": [
        ("中国新闻网-国际", "https://www.chinanews.com.cn/rss/world.xml"),
        ("联合早报-国际", "https://plink.anyfeeder.com/zaobao/realtime/world"),
    ],
    "tech": [
        ("IT之家", "https://www.ithome.com/rss/"),
        ("36氪", "https://36kr.com/feed"),
    ],
    "ai": [
        ("机器之心", "https://www.jiqizhixin.com/rss"),
        ("Hacker News", "https://hnrss.org/frontpage?count=15"),
    ],
    "business": [
        ("中国新闻网-财经", "https://www.chinanews.com.cn/rss/finance.xml"),
        ("财联社/华尔街见闻", "https://plink.anyfeeder.com/wallstreetcn/news/global"),
    ],
}

SYSTEM_PROMPT = """# Role
你是一名高信息密度的每日情报编辑，负责将过去 24 小时的原始新闻素材提炼为极简、客观、无废话的每日热点简报。

# 分类规范（严格使用以下 5 个板块 id 和 name）
1. domestic -> 国内时政/社会
2. world -> 国际动态
3. tech -> 科技/互联网
4. ai -> AI与政策
5. business -> 财经/商业

# 核心写作法则（必须严格遵守）
1. 增量写作法（严禁标题与摘要重复）：
   - title（标题）：≤ 22 字，直接给核心主体与结论。
   - summary（摘要）：≤ 45 字，只写标题里没有的增量信息（具体数据、时间地点、深层原因、后续结果）。绝对禁止复述 title 的主语或前半句！
2. 重要性分级（importance）：
   - 每个板块筛选 4~5 条最有价值的事件（全天总计 20~25 条）。
   - 每条打上 "high" 或 "normal" 标签，全天 "high" 严格控制在 3~5 条以内（仅限大国元首级外交、破纪录商业/科技里程碑、核心资产剧烈波动、重大政策转向或突发灾害）。
3. 一句话看点（impact）：
   - 仅针对 high 级别以及科技/AI/财经中有长期趋势价值的条目，用 ≤ 25 字补充潜在影响；其余留空字符串 ""。
4. 今日 Top 3（top_3）：
   - 从 high 级别条目中挑选最重磅的 3 条，每条用 ≤ 30 字浓缩成极简快讯，并关联对应原条目的 id。
5. 链接保真：
   - url 必须严格使用素材中提供的原始链接，禁止编造链接。

# Output Format
仅输出合法的 JSON 对象，不要包含任何解释性文字：
{
  "date": "YYYY-MM-DD",
  "top_3": [
    { "ref_id": "world-01", "headline": "极简一句话速览" }
  ],
  "sections": [
    {
      "id": "domestic",
      "name": "国内时政/社会",
      "items": [
        {
          "id": "domestic-01",
          "importance": "high",
          "title": "...",
          "summary": "...",
          "impact": "...",
          "source_name": "来源媒体名",
          "url": "原始链接"
        }
      ]
    }
  ]
}"""


def fetch_rss_items(url: str, source_name: str, limit: int = 8) -> list[dict]:
    items = []
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; DailyBriefBot/1.0)"},
        )
        with urllib.request.urlopen(req, timeout=12) as resp:
            raw_xml = resp.read()
        root = ET.fromstring(raw_xml)
        # 兼容 RSS 2.0 (<item>) 和 Atom (<entry>)
        nodes = root.findall(".//item") or root.findall(".//{http://www.w3.org/2005/Atom}entry")
        for node in nodes[:limit]:
            title = (
                node.findtext("title")
                or node.findtext("{http://www.w3.org/2005/Atom}title")
                or ""
            ).strip()
            link = (node.findtext("link") or "").strip()
            if not link:
                link_el = node.find("{http://www.w3.org/2005/Atom}link")
                if link_el is not None:
                    link = link_el.attrib.get("href", "").strip()
            desc = (
                node.findtext("description")
                or node.findtext("{http://www.w3.org/2005/Atom}summary")
                or ""
            ).strip()
            # 简单清理 HTML 标签并截断，节省 Token
            import re
            desc = re.sub(r"<[^>]+>", "", desc)
            desc = re.sub(r"\s+", " ", desc)[:160]
            if title and link:
                items.append({
                    "source": source_name,
                    "title": title,
                    "desc": desc,
                    "url": link,
                })
    except Exception as e:
        print(f"[警告] 抓取失败 {source_name} ({url}): {e}", file=sys.stderr)
    return items


def call_deepseek(raw_news_text: str, today_str: str) -> dict:
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    base_url = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
    model = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")

    if not api_key:
        raise RuntimeError("未检测到环境变量 DEEPSEEK_API_KEY，请先在 GitHub Secrets 中配置。")

    payload = {
        "model": model,
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": f"今天是 {today_str}。请根据以下最新抓取的原始新闻素材，去重筛选并生成今日简报 JSON：\n\n{raw_news_text}",
            },
        ],
    }

    req = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )

    with urllib.request.urlopen(req, timeout=120) as resp:
        res_data = json.loads(resp.read().decode("utf-8"))

    content = res_data["choices"][0]["message"]["content"].strip()
    # 兼容可能包裹的 markdown 代码块
    if content.startswith("```"):
        content = content.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    return json.loads(content)


def main():
    parser = argparse.ArgumentParser(description="抓取 RSS 并调用 DeepSeek 生成 brief.json")
    parser.add_argument("-o", "--output", default="brief.json", help="输出的 JSON 文件路径")
    args = parser.parse_args()

    tz_cn = timezone(timedelta(hours=8))
    today_str = datetime.now(tz_cn).strftime("%Y-%m-%d")

    all_blocks = []
    total_fetched = 0
    for cat, feeds in RSS_FEEDS.items():
        cat_lines = [f"=== 候选分类: {cat} ==="]
        for source_name, url in feeds:
            entries = fetch_rss_items(url, source_name, limit=8)
            total_fetched += len(entries)
            for e in entries:
                cat_lines.append(
                    f"- [{e['source']}] {e['title']} | 摘要: {e['desc']} | 链接: {e['url']}"
                )
        all_blocks.append("\n".join(cat_lines))

    print(f"共抓取到 {total_fetched} 条原始新闻，正在调用 DeepSeek 提炼...")
    if total_fetched == 0:
        raise RuntimeError("所有 RSS 源均抓取失败，请检查网络或更换源。")

    brief_data = call_deepseek("\n\n".join(all_blocks), today_str)
    brief_data["date"] = today_str

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(brief_data, f, ensure_ascii=False, indent=2)
    print(f"已成功生成 {args.output}")


if __name__ == "__main__":
    main()
