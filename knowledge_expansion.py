#!/usr/bin/env python3
"""为 LocalAI Tiny 批量扩充可追溯的三语通用知识。

数据来自 MediaWiki Action API。导入器只使用 Python 标准库，支持重复执行、
断点续跑、按主题限额，并在 SQLite 层设置硬页数上限，防止主库超过 600 MiB。
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from localai_extensions import ensure_extended_knowledge, normalize_text


DATABASE_MAX_BYTES = 600 * 1024 * 1024
# 给回滚日志、SQLite 元数据增长和最后一个事务留出空间；主数据库本身更早停止。
DATABASE_PAGE_BUDGET = 592 * 1024 * 1024
USER_AGENT = "LocalAITinyKnowledgeBuilder/1.0 (local educational knowledge database)"
ZH_API = "https://zh.wikipedia.org/w/api.php"
EN_API = "https://en.wikipedia.org/w/api.php"
WIKIDATA_API = "https://www.wikidata.org/w/api.php"
MIN_REQUEST_INTERVAL = .8
_last_request_at = 0.0

# 每个领域列出若干直接相关分类，避免从过宽的根分类无边界地爬取人物、模板等内容。
# 分类不存在时 API 会返回空集合，不影响其余分类。
TOPIC_CATEGORIES: dict[str, tuple[str, ...]] = {
    "biology": (
        "生物学", "细胞生物学", "遗传学", "生态学", "动物学", "植物学",
        "微生物学", "分子生物学", "生物化学", "人体",
    ),
    "geography": (
        "地理学", "自然地理学", "地形", "海洋", "河流", "湖泊", "山脉",
        "国家", "亚洲地理", "欧洲地理", "非洲地理", "美洲地理", "大洋洲地理",
    ),
    "technology": (
        "技术", "信息技术", "互联网", "人工智能", "通信技术", "电子技术",
        "数字技术", "机器人学", "网络技术",
    ),
    "computing": (
        "计算机科学", "计算机硬件", "软件", "编程语言", "操作系统", "数据库",
        "计算机网络", "信息安全", "数据结构",
    ),
    "physics": (
        "物理学", "经典力学", "热力学", "电磁学", "光学", "声学", "量子力学",
        "相对论", "物理量",
    ),
    "chemistry": (
        "化学", "化学元素", "无机化学", "有机化学", "分析化学", "物理化学",
        "化学反应", "化合物",
    ),
    "astronomy": (
        "天文学", "太阳系", "恒星", "行星", "星系", "天体", "宇宙学",
    ),
    "earth_environment": (
        "地球科学", "地质学", "气象学", "气候学", "环境科学", "自然灾害",
        "环境保护", "气候变化",
    ),
    "engineering": (
        "工程学", "机械工程", "电机工程", "土木工程", "材料科学", "建筑工程",
        "制造业", "自动化",
    ),
    "health": (
        "基础医学", "人体解剖学", "生理学", "营养学", "公共卫生", "急救",
        "传染病学", "健康",
    ),
    "mathematics": (
        "数学", "算术", "代数学", "几何学", "概率论", "统计学", "函数",
        "数论",
    ),
    "transport_energy": (
        "交通", "交通工具", "铁路运输", "航空", "航海", "能源", "可再生能源",
        "发电", "电池",
    ),
    "daily_life": (
        "食品", "烹饪", "家用电器", "农业", "计量单位", "时间", "历法",
        "常识",
    ),
}

TOPIC_WEIGHTS = {
    "biology": 1.35, "geography": 1.35, "technology": 1.25, "computing": 1.15,
    "physics": .80, "chemistry": .80, "astronomy": .60, "earth_environment": .80,
    "engineering": .75, "health": .75, "mathematics": .65,
    "transport_energy": .60, "daily_life": .55,
}

TITLE_EXCLUSIONS = re.compile(
    r"(?:列表|清单|索引|年表|大纲|模板|分类|条目|主题列表|消歧义|专题|门户|作品列表)$"
)


def database_disk_bytes(db_path: Path) -> int:
    """统计主库及可能存在的 WAL/SHM；文件不存在时按 0 处理。"""
    return sum(path.stat().st_size for path in
               (db_path, Path(str(db_path) + "-wal"), Path(str(db_path) + "-shm"))
               if path.exists())


def _api(endpoint: str, parameters: dict[str, object], retries: int = 4) -> dict:
    global _last_request_at
    params = {"format": "json", "formatversion": "2", "utf8": "1", **parameters}
    # Wikipedia 正文读取尊重 maxlag；Wikidata 标签服务偶尔报告数分钟复制延迟，
    # 标签并非时效数据，此时读取稍旧副本优于丢掉整批简繁标题。
    if endpoint != WIKIDATA_API:
        params["maxlag"] = "5"
    request = urllib.request.Request(
        endpoint + "?" + urllib.parse.urlencode(params),
        headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity"},
    )
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            # 共享网络出口可能有其他程序同时访问 Wikimedia。主动节流既遵守接口
            # 礼仪，也避免大量分类请求把后续正文请求挤进 429 限流窗口。
            delay = MIN_REQUEST_INTERVAL - (time.monotonic() - _last_request_at)
            if delay > 0:
                time.sleep(delay)
            with urllib.request.urlopen(request, timeout=35) as response:
                _last_request_at = time.monotonic()
                payload = json.load(response)
            if "error" in payload:
                raise RuntimeError(payload["error"].get("info", str(payload["error"])))
            return payload
        except urllib.error.HTTPError as error:
            _last_request_at = time.monotonic()
            last_error = error
            if attempt + 1 < retries:
                retry_after = error.headers.get("Retry-After", "") if error.headers else ""
                try:
                    wait = float(retry_after)
                except ValueError:
                    wait = 20 if error.code == 429 else min(2 ** attempt, 6)
                time.sleep(min(max(wait, 2), 60))
        except (OSError, json.JSONDecodeError, RuntimeError) as error:
            _last_request_at = time.monotonic()
            last_error = error
            if attempt + 1 < retries:
                time.sleep(min(2 ** attempt, 6))
    raise RuntimeError(f"MediaWiki API 请求失败：{last_error}")


def _chunks(values: list, size: int = 20):
    for start in range(0, len(values), size):
        yield values[start:start + size]


def _category_pages(category: str, limit: int) -> list[str]:
    """获取分类中的正文页；使用 continuation，且绝不进入模板/文件命名空间。"""
    found: list[str] = []
    continuation: dict[str, str] = {}
    while len(found) < limit:
        payload = _api(ZH_API, {
            "action": "query", "list": "categorymembers", "cmtitle": f"Category:{category}",
            "cmnamespace": "0", "cmtype": "page", "cmprop": "ids|title|type",
            "cmlimit": min(500, limit - len(found)), **continuation,
        })
        for member in payload.get("query", {}).get("categorymembers", []):
            title = member.get("title", "").strip()
            if title and ":" not in title and not TITLE_EXCLUSIONS.search(title):
                found.append(title)
        continuation = payload.get("continue", {})
        if not continuation:
            break
    return found[:limit]


def _topic_target(topic: str, total_limit: int) -> int:
    weight_sum = sum(TOPIC_WEIGHTS.values())
    return max(20, math.ceil(total_limit * TOPIC_WEIGHTS[topic] / weight_sum))


def _collect_topic_candidates(topic: str, total_limit: int) -> list[str]:
    """轮询一个领域的分类；调用方可在每个领域之后立即提交数据库。"""
    categories = TOPIC_CATEGORIES[topic]
    target = _topic_target(topic, total_limit)
    per_category = min(80, max(12, math.ceil(target * 1.8 / len(categories))))
    seen: set[str] = set()
    buckets: list[list[str]] = []
    for category in categories:
        try:
            buckets.append(_category_pages(category, per_category))
        except RuntimeError as error:
            print(f"  ! 分类 {category} 暂时不可用：{error}")
            buckets.append([])
    selected: list[str] = []
    for index in range(max((len(bucket) for bucket in buckets), default=0)):
        for bucket in buckets:
            if index < len(bucket) and bucket[index] not in seen:
                seen.add(bucket[index]); selected.append(bucket[index])
                if len(selected) >= target:
                    break
        if len(selected) >= target:
            break
    print(f"  {topic}: 候选 {len(selected):,} 条，开始三语过滤与写入")
    return selected


def _query_zh_pages(titles: list[str], variant: str) -> list[dict]:
    payload = _api(ZH_API, {
        "action": "query", "prop": "extracts|langlinks|info|pageprops|revisions",
        "titles": "|".join(titles), "redirects": "1", "variant": variant,
        "exintro": "1", "explaintext": "1", "exchars": "1200", "inprop": "url",
        # lllimit 是整批 query 的属性限制，不是每页限制；设为 1 会导致每批只有
        # 第一页拿到英文链接。
        "lllang": "en", "lllimit": "max", "ppprop": "disambiguation|wikibase_item",
        "rvprop": "ids|timestamp",
    })
    return payload.get("query", {}).get("pages", [])


def _query_zh_pageids(pageids: list[int], variant: str) -> dict[int, dict]:
    if not pageids:
        return {}
    payload = _api(ZH_API, {
        "action": "query", "prop": "extracts|info|revisions", "pageids": "|".join(map(str, pageids)),
        "variant": variant, "exintro": "1", "explaintext": "1", "exchars": "1200",
        "inprop": "url", "rvprop": "ids|timestamp",
    })
    return {page["pageid"]: page for page in payload.get("query", {}).get("pages", [])
            if "pageid" in page}


def _query_en_pages(titles: list[str]) -> dict[str, dict]:
    if not titles:
        return {}
    payload = _api(EN_API, {
        "action": "query", "prop": "extracts|info|revisions", "titles": "|".join(titles),
        "redirects": "1", "exintro": "1", "explaintext": "1", "exchars": "1200",
        "inprop": "url", "rvprop": "ids|timestamp",
    })
    pages = payload.get("query", {}).get("pages", [])
    result = {normalize_text(page.get("title", "")): page for page in pages if "missing" not in page}
    # 记录重定向前的英文标题，使 langlink 指向重定向时仍能配回中文页。
    for redirect in payload.get("query", {}).get("redirects", []):
        target = result.get(normalize_text(redirect.get("to", "")))
        if target:
            result[normalize_text(redirect.get("from", ""))] = target
    return result


def _query_wikidata_labels(entity_ids: list[str]) -> dict[str, dict[str, str]]:
    """批量取得可靠的简体/繁体标题；缺少标签时由 Wikipedia 原标题回退。"""
    if not entity_ids:
        return {}
    try:
        payload = _api(WIKIDATA_API, {
            "action": "wbgetentities", "ids": "|".join(entity_ids), "props": "labels",
            "languages": "zh-hans|zh-hant|zh|en", "languagefallback": "1",
        }, retries=2)
    except RuntimeError:
        # 标签只是标题增强，正文和英文对应页均由 Wikipedia 提供；Wikidata
        # maxlag 时不能让整批可靠摘要一起失败。
        return {}
    result: dict[str, dict[str, str]] = {}
    for entity_id, entity in payload.get("entities", {}).items():
        labels = entity.get("labels", {})
        result[entity_id] = {language: value.get("value", "")
                             for language, value in labels.items()}
    return result


def _clean_extract(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _revision(page: dict) -> str:
    revision = (page.get("revisions") or [{}])[0]
    revision_id = revision.get("revid", "unknown")
    timestamp = revision.get("timestamp", "")
    return f"{revision_id}@{timestamp}" if timestamp else str(revision_id)


def _prepare_records(topic: str, titles: list[str]) -> list[dict]:
    records: list[dict] = []
    for batch in _chunks(titles):
        hans_pages = _query_zh_pages(batch, "zh-hans")
        valid = [page for page in hans_pages if "missing" not in page and "pageid" in page
                 and "disambiguation" not in page.get("pageprops", {})]
        hant_by_id = _query_zh_pageids([page["pageid"] for page in valid], "zh-hant")
        entity_ids = [page.get("pageprops", {}).get("wikibase_item", "") for page in valid]
        labels_by_id = _query_wikidata_labels([item for item in entity_ids if item])
        english_titles = []
        for page in valid:
            links = page.get("langlinks") or []
            if links and links[0].get("title"):
                english_titles.append(links[0]["title"])
        english_by_title = _query_en_pages(english_titles)

        for page in valid:
            links = page.get("langlinks") or []
            if not links:
                continue
            en_link = links[0].get("title", "")
            hant = hant_by_id.get(page["pageid"])
            english = english_by_title.get(normalize_text(en_link))
            if not hant or not english:
                continue
            zh_summary = _clean_extract(page.get("extract", ""))
            hant_summary = _clean_extract(hant.get("extract", ""))
            en_summary = _clean_extract(english.get("extract", ""))
            if min(len(zh_summary), len(hant_summary), len(en_summary)) < 60:
                continue
            item_id = page.get("pageprops", {}).get("wikibase_item", "")
            labels = labels_by_id.get(item_id, {})
            title_zh = (labels.get("zh-hans") or labels.get("zh") or page.get("title", "")).strip()
            title_hant = (labels.get("zh-hant") or labels.get("zh") or hant.get("title", "")).strip()
            title_en = (labels.get("en") or english.get("title", "")).strip()
            if not all((title_zh, title_hant, title_en)):
                continue
            records.append({
                "entry_id": f"wikipedia_zh_{page['pageid']}", "category": topic,
                "title_zh": title_zh, "title_hant": title_hant, "title_en": title_en,
                "summary_zh": zh_summary, "summary_hant": hant_summary,
                "summary_en": en_summary,
                "source": "Wikipedia contributors (CC BY-SA 4.0)",
                "source_url": page.get("fullurl", ""),
                "revision": f"zh:{_revision(page)}; en:{_revision(english)}",
                "confidence": .82,
                "aliases": {page.get("title", ""), hant.get("title", ""), en_link,
                            english.get("title", "")},
                "attribution": {
                    "zh": (page.get("fullurl", ""), _revision(page)),
                    "zh_hant": (hant.get("fullurl", page.get("fullurl", "")), _revision(hant)),
                    "en": (english.get("fullurl", ""), _revision(english)),
                },
            })
    return records


def _ensure_import_schema(con: sqlite3.Connection, max_bytes: int) -> None:
    page_size = con.execute("PRAGMA page_size").fetchone()[0]
    page_budget = min(DATABASE_PAGE_BUDGET, max_bytes - 8 * 1024 * 1024)
    current_pages = con.execute("PRAGMA page_count").fetchone()[0]
    max_pages = page_budget // page_size
    if current_pages >= max_pages:
        raise RuntimeError("数据库已达到安全容量上限，未继续写入。")
    # SQLite 会持久化较小的 max_page_count；即使导入器异常也不会越过此界线。
    con.execute(f"PRAGMA max_page_count={max_pages}")
    con.execute("""CREATE TABLE IF NOT EXISTS encyclopedia_attribution(
      entry_id TEXT NOT NULL, language TEXT NOT NULL, source_name TEXT NOT NULL,
      source_url TEXT NOT NULL, license TEXT NOT NULL, revision TEXT NOT NULL,
      imported_at TEXT NOT NULL, PRIMARY KEY(entry_id,language,source_url))""")
    con.execute("""CREATE TABLE IF NOT EXISTS knowledge_import_log(
      import_id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL,
      finished_at TEXT NOT NULL, provider TEXT NOT NULL, requested INTEGER NOT NULL,
      imported INTEGER NOT NULL, skipped INTEGER NOT NULL, database_bytes INTEGER NOT NULL,
      status TEXT NOT NULL)""")
    con.commit()


def _store_records(con: sqlite3.Connection, db_path: Path, records: list[dict],
                   max_bytes: int) -> tuple[int, int]:
    imported = skipped = 0
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    fts_available = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='encyclopedia_fts'").fetchone()
    for record in records:
        if database_disk_bytes(db_path) >= max_bytes - 8 * 1024 * 1024:
            raise RuntimeError("数据库接近 600 MiB 上限，导入已安全停止。")
        old = con.execute("""SELECT category,title_zh,title_hant,title_en,
          summary_zh,summary_hant,summary_en,revision FROM encyclopedia_entries
          WHERE entry_id=?""", (record["entry_id"],)).fetchone()
        current = tuple(record[key] for key in (
            "category", "title_zh", "title_hant", "title_en", "summary_zh",
            "summary_hant", "summary_en", "revision"))
        if old == current:
            skipped += 1
            continue
        values = (record[key] for key in (
            "entry_id", "category", "title_zh", "title_hant", "title_en",
            "summary_zh", "summary_hant", "summary_en", "source", "source_url",
            "revision", "confidence"))
        con.execute("INSERT OR REPLACE INTO encyclopedia_entries VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    tuple(values))
        con.execute("DELETE FROM encyclopedia_terms WHERE entry_id=?", (record["entry_id"],))
        for language, title in (("zh", record["title_zh"]),
                                ("zh_hant", record["title_hant"]),
                                ("en", record["title_en"])):
            con.execute("INSERT OR REPLACE INTO encyclopedia_terms VALUES(?,?,?,?,?)",
                        (record["entry_id"], language, title, normalize_text(title), "title"))
        for alias in record.get("aliases", set()):
            if not alias:
                continue
            language = "en" if re.search(r"[A-Za-z]", alias) and not re.search(r"[\u3400-\u9fff]", alias) else "zh"
            con.execute("INSERT OR IGNORE INTO encyclopedia_terms VALUES(?,?,?,?,?)",
                        (record["entry_id"], language, alias, normalize_text(alias), "alias"))
        # 简繁标题互作别名，让用户输入任一字形都可在另一界面命中。
        for language, alias in (("zh", record["title_hant"]),
                                ("zh_hant", record["title_zh"])):
            con.execute("INSERT OR IGNORE INTO encyclopedia_terms VALUES(?,?,?,?,?)",
                        (record["entry_id"], language, alias, normalize_text(alias), "alias"))
        con.execute("INSERT OR REPLACE INTO knowledge_sources VALUES(?,?,?,?,?,?,?)", (
            "encyclopedia", record["entry_id"], record["source"], record["source_url"],
            record["revision"], record["confidence"], now))
        con.execute("DELETE FROM encyclopedia_attribution WHERE entry_id=?", (record["entry_id"],))
        for language, (url, revision) in record["attribution"].items():
            con.execute("INSERT OR REPLACE INTO encyclopedia_attribution VALUES(?,?,?,?,?,?,?)", (
                record["entry_id"], language, "Wikipedia contributors", url,
                "CC BY-SA 4.0", revision, now))
        if fts_available:
            con.execute("DELETE FROM encyclopedia_fts WHERE entry_id=?", (record["entry_id"],))
            for language, title, body in (
                ("zh", record["title_zh"], record["summary_zh"]),
                ("zh_hant", record["title_hant"], record["summary_hant"]),
                ("en", record["title_en"], record["summary_en"]),
            ):
                con.execute("INSERT INTO encyclopedia_fts VALUES(?,?,?,?,?)",
                            (record["entry_id"], language, title, "", body))
        imported += 1
        if imported % 25 == 0:
            con.commit()
    con.commit()
    return imported, skipped


def import_general_knowledge(db_path: Path, limit: int = 1500,
                             max_bytes: int = DATABASE_MAX_BYTES,
                             topics: tuple[str, ...] | None = None) -> dict[str, object]:
    """导入生物、地理、科技及通用百科，返回可打印统计。"""
    if limit < 1:
        raise ValueError("limit 必须大于 0")
    if max_bytes > DATABASE_MAX_BYTES:
        raise ValueError("本项目不允许把知识库上限设为 600 MiB 以上")
    selected_topics = topics or tuple(TOPIC_CATEGORIES)
    unknown_topics = set(selected_topics) - set(TOPIC_CATEGORIES)
    if unknown_topics:
        raise ValueError("未知知识领域：" + ", ".join(sorted(unknown_topics)))
    ensure_extended_knowledge(db_path)
    if database_disk_bytes(db_path) >= max_bytes:
        raise RuntimeError("数据库已经达到 600 MiB 上限")
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    con = sqlite3.connect(db_path)
    _ensure_import_schema(con, max_bytes)
    existing = {row[0] for row in con.execute(
        "SELECT entry_id FROM encyclopedia_entries WHERE entry_id LIKE 'wikipedia_zh_%'")}
    print(f"开始扩充通用知识（目标最多 {limit:,} 条，已有 Wikipedia 条目 {len(existing):,}）……")
    imported_total = skipped_total = 0
    errors: list[str] = []
    for topic in selected_topics:
        if imported_total >= limit:
            break
        try:
            titles = _collect_topic_candidates(topic, limit)
            records = _prepare_records(topic, titles[:limit - imported_total])
            imported, skipped = _store_records(con, db_path, records, max_bytes)
            imported_total += imported; skipped_total += skipped
            print(f"  ✓ {topic}: 写入/更新 {imported:,}，未变化 {skipped:,}")
        except RuntimeError as error:
            errors.append(f"{topic}: {error}")
            print(f"  ! {topic}: {error}")
            if "上限" in str(error):
                break
    finished = datetime.now(timezone.utc).isoformat(timespec="seconds")
    db_bytes = database_disk_bytes(db_path)
    status = "partial" if errors else "ok"
    con.execute("""INSERT INTO knowledge_import_log
      (started_at,finished_at,provider,requested,imported,skipped,database_bytes,status)
      VALUES(?,?,?,?,?,?,?,?)""",
      (started, finished, "Wikimedia Action API", limit, imported_total,
       skipped_total, db_bytes, status))
    con.commit()
    counts = dict(con.execute(
        "SELECT category,count(*) FROM encyclopedia_entries GROUP BY category ORDER BY category"))
    total = con.execute("SELECT count(*) FROM encyclopedia_entries").fetchone()[0]
    con.close()
    if db_bytes > DATABASE_MAX_BYTES:
        raise RuntimeError("安全检查失败：数据库文件超过 600 MiB")
    return {"imported": imported_total, "skipped": skipped_total, "total": total,
            "database_bytes": db_bytes, "categories": counts, "errors": errors}


def knowledge_statistics(db_path: Path) -> dict[str, object]:
    ensure_extended_knowledge(db_path)
    con = sqlite3.connect(db_path)
    categories = dict(con.execute(
        "SELECT category,count(*) FROM encyclopedia_entries GROUP BY category ORDER BY category"))
    result = {
        "entries": sum(categories.values()), "categories": categories,
        "terms": con.execute("SELECT count(*) FROM encyclopedia_terms").fetchone()[0],
        "fts_rows": con.execute("SELECT count(*) FROM encyclopedia_fts").fetchone()[0],
        "sources": con.execute("SELECT count(*) FROM knowledge_sources WHERE entity_type='encyclopedia'").fetchone()[0],
        "database_bytes": database_disk_bytes(db_path), "max_bytes": DATABASE_MAX_BYTES,
    }
    con.close()
    return result
