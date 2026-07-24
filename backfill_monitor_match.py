"""
识别群监控配置一次性回补脚本

背景：识别群自动匹配（insurance/auto_monitor_match.py）上线于 2026-07-22，
而 task_group_sync.py 过去只在「群名变化(name_changed)」时才调 check_and_sync_group。
于是上线前就已存在、之后群名再没变过的存量识别群，永远不会被自动加入监控配置。

本脚本扫描 wecom_groups 全表，对命中「识别群 + TZ编号」规则的群做补齐：
  - 默认 dry-run：只读，打印将要新增/已存在/需建占位企业的清单，不写任何库
  - 加 --apply：对「将新增」和「编号未匹配企业」的群实际调 check_and_sync_group 写库
                （复用线上幂等逻辑：判重、必要时建占位企业+发钉钉通知、加入监控配置）

用法（须在项目根目录 /home/wxbot 或本地项目根执行，依赖同目录 config.yaml）：
    python3 backfill_monitor_match.py            # 检查（dry-run，只读）
    python3 backfill_monitor_match.py --apply    # 实际回补写库

注：task_group_sync.py 已改为「每次全量刷新都对识别群幂等补齐」，长期由定时任务兜底；
    本脚本仅用于「立即补齐存量、不想等下一轮全量（默认 6h）」的场景。
"""

import sys

import yaml

from storage.db import Database
from auth.db import get_enterprise_by_no
from insurance.monitor_config_db import list_monitor_configs_by_enterprise
# 复用线上规则常量与幂等入口，保证判定与自动匹配完全一致，避免规则漂移
from insurance.auto_monitor_match import (
    _RECOGNIZE_KEYWORD, _TZ_PATTERN, check_and_sync_group,
)


def _classify(db, roomid, name):
    """按线上规则对单个群分类，返回 (状态, tz_key, enterprise)。
    状态：None=非识别群 / already=已在配置 / will_add=命中企业但未加入 / no_enterprise=编号未匹配企业"""
    if not name or _RECOGNIZE_KEYWORD not in name:
        return None, None, None
    m = _TZ_PATTERN.search(name)
    if not m:
        return None, None, None
    tz_key = "TZ" + m.group(1).zfill(3)          # 数字补齐 3 位，与线上归一化一致

    enterprise = get_enterprise_by_no(db, tz_key)
    if not enterprise:
        return "no_enterprise", tz_key, None

    configs = list_monitor_configs_by_enterprise(db, enterprise["id"])
    for c in configs:
        room_ids = {r.get("id") for r in (c.get("rooms") or []) if isinstance(r, dict)}
        if roomid in room_ids:
            return "already", tz_key, enterprise
    return "will_add", tz_key, enterprise


def main():
    apply = "--apply" in sys.argv[1:]

    with open("config.yaml", "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    # --apply 时初始化钉钉通知器（建占位企业会发通知，与线上行为一致）；dry-run 不需要
    if apply:
        from core.notify import init_notifier
        notify_cfg = config.get("notify", {}) or {}
        init_notifier(notify_cfg.get("webhook_url", ""), notify_cfg.get("secret", ""))

    db = Database(config["mysql"], main_corpid=config["wecom"]["corpid"])

    # 扫全表所有已解析出群名的群
    conn = db.pool.connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT roomid, name, corpid FROM wecom_groups WHERE name != ''")
        rows = list(cur.fetchall())
    finally:
        conn.close()

    already, will_add, no_enterprise = [], [], []
    for r in rows:
        # SELECT 顺序为 roomid, name, corpid；默认游标返回 tuple，按列索引取值
        roomid, name, corpid = r[0], r[1], r[2] or ""
        status, tz_key, ent = _classify(db, roomid, name)
        if status == "already":
            already.append((roomid, name, tz_key, ent["name"]))
        elif status == "will_add":
            will_add.append((roomid, name, corpid, tz_key, ent["name"]))
        elif status == "no_enterprise":
            no_enterprise.append((roomid, name, corpid, tz_key))

    mode = "APPLY（写库）" if apply else "DRY-RUN（只读检查）"
    print(f"\n===== 识别群监控配置回补 [{mode}] =====")
    print(f"扫描群总数: {len(rows)}")
    print(f"识别群命中规则: {len(already) + len(will_add) + len(no_enterprise)}")
    print(f"  - 已在监控配置(跳过): {len(already)}")
    print(f"  - 命中企业待加入   : {len(will_add)}")
    print(f"  - 编号未匹配企业(将建占位企业): {len(no_enterprise)}")

    if will_add:
        print("\n--- [待加入现有企业监控配置] ---")
        for roomid, name, corpid, tz_key, ent_name in will_add:
            print(f"  [{tz_key}] {name}  ->  企业「{ent_name}」  ({roomid})")

    if no_enterprise:
        print("\n--- [编号未匹配企业，将自动创建占位企业] ---")
        for roomid, name, corpid, tz_key in no_enterprise:
            print(f"  [{tz_key}] {name}  ({roomid})")

    to_write = will_add + [(r[0], r[1], r[2], r[3], None) for r in no_enterprise]
    if not apply:
        print(f"\n[DRY-RUN] 本次为只读检查，未写入任何数据。")
        print(f"[DRY-RUN] 确认无误后执行: python3 backfill_monitor_match.py --apply")
        print(f"[DRY-RUN] 届时将实际回补 {len(to_write)} 个群。\n")
        return

    print(f"\n[APPLY] 开始回补 {len(to_write)} 个群 ...")
    done = 0
    for item in to_write:
        roomid, name, corpid = item[0], item[1], item[2]
        # 直接复用线上幂等入口：内部判重、必要时建占位企业+发钉钉通知、加入监控配置
        check_and_sync_group(db, roomid, name, corpid)
        done += 1
    print(f"[APPLY] 回补完成，共处理 {done} 个群（具体加入/新建详情见上方各群日志）。\n")


if __name__ == "__main__":
    main()
