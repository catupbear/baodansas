#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
客户跟进日报：每天 22:00 推送当天「客户档案有更新」的客户跟进到钉钉群。

- 只推送 enterprises.profile_updated_at 为当天的企业（即当天有人改过客户档案：
  跟进状态 / 跟进记录 / 联系资料 / 接入时间 / 推荐人）；当天无更新则不推送。
- 钉钉机器人凭据存 DB：insurance_config['customer_followup_notify'] = {webhook_url, secret}（加签）。
- 由 crontab 每天 22:00 触发。
"""
import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pymysql
import yaml

from storage.db import Database
from insurance.db import get_insurance_config
from core.notify import DingTalkNotifier

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

CONFIG_PATH = "/home/wxbot/config.yaml"
NOTIFY_KEY = "customer_followup_notify"


def load_config(path=CONFIG_PATH):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def fetch_today_followups(db, today_str):
    """查「今天新增了跟进记录」的企业：跟进记录(remark JSON)里存在 date==今天 的条目。
    只认新增跟进记录，状态/联系人等其它档案改动不算。每个企业取其今天最新的一条跟进。"""
    conn = db.pool.connection()
    try:
        cur = conn.cursor(pymysql.cursors.DictCursor)
        # 先用 LIKE 今天日期粗筛（remark 里含今天日期串），再解析确认，避免全表解析
        cur.execute(
            "SELECT name, enterprise_no, follow_status, remark, profile_updated_at "
            "FROM enterprises WHERE remark LIKE %s ORDER BY profile_updated_at DESC",
            (f"%{today_str}%",),
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    result = []
    for r in rows:
        try:
            rl = json.loads(r.get("remark") or "[]")
        except (TypeError, ValueError):
            rl = []
        # 今天的跟进条目（date==今天）；跟进记录按最新在前，取第一条今天的
        today_notes = [
            x for x in rl
            if isinstance(x, dict) and (x.get("date") or "").strip() == today_str
        ]
        if today_notes:
            r["_today_note"] = today_notes[0]
            result.append(r)
    return result


def build_message(rows, today_str):
    """拼钉钉 markdown 正文。"""
    if not rows:
        return f"### 📋 今日客户跟进更新（{today_str}）\n\n今日暂无客户跟进更新。"

    lines = [
        f"### 📋 今日客户跟进更新（{today_str}）",
        "",
        f"今天共 **{len(rows)}** 个客户新增了跟进：",
        "",
    ]
    for i, r in enumerate(rows, 1):
        name = r.get("name") or "-"
        no = (r.get("enterprise_no") or "").strip()
        st = r.get("follow_status") or "未标记"
        title = f"{name}（{no}）" if no else name
        lines.append(f"**{i}. {title}** — {st}")
        f = r.get("_today_note") or {}
        d = (f.get("date") or "").strip()
        lines.append(f"> {d} 跟进：{f.get('content', '')}" if d else f"> 跟进：{f.get('content', '')}")
        lines.append("")
    return "\n".join(lines).rstrip()


def main():
    from datetime import date
    today_str = date.today().strftime("%Y-%m-%d")
    db = Database(load_config()["mysql"])
    rows = fetch_today_followups(db, today_str)

    cfg = get_insurance_config(db, NOTIFY_KEY, {}) or {}
    webhook = cfg.get("webhook_url", "")
    secret = cfg.get("secret", "")
    if not webhook:
        logger.warning("未配置 %s 的 webhook，跳过推送（共 %d 个新增跟进）", NOTIFY_KEY, len(rows))
        return

    # 当天无新增跟进也照发一句「暂无更新」，不静默跳过
    msg = build_message(rows, today_str)
    DingTalkNotifier(webhook, secret).send("今日客户跟进更新", msg, dedup=False)
    logger.info("已推送今日客户跟进更新，共 %d 个客户", len(rows))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        logger.exception("客户跟进日报任务异常退出")
        raise
