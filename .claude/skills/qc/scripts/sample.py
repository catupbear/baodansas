# -*- coding: utf-8 -*-
"""保单识别质检 - 抽样脚本（在生产服务器上跑）。
取昨天入库、已识别完成、有 cos_url 的记录，按保司分组每组随机抽最多 5 份，
写出 /tmp/qc_sample.json 供本地下载 PDF + Claude 比对。只读，不改任何数据。"""
import yaml, pymysql, json, random

PER_COMPANY = 5  # 每个保司抽多少份

cfg = yaml.safe_load(open("/home/wxbot/config.yaml"))["mysql"]
conn = pymysql.connect(host=cfg["host"], port=int(cfg.get("port", 3306)),
                       user=cfg["user"], password=cfg["password"],
                       database=cfg["database"], charset="utf8mb4")
cur = conn.cursor(pymysql.cursors.DictCursor)
cur.execute("""
    SELECT id, company_short, filename, cos_url, policy_count, policy_index, display_fields
    FROM insurance_records
    WHERE DATE(created_at) = DATE_SUB(CURDATE(), INTERVAL 1 DAY)
      AND deleted_at IS NULL
      AND status = 'done'
      AND cos_url IS NOT NULL AND cos_url <> ''
""")
rows = cur.fetchall()

groups = {}
for r in rows:
    groups.setdefault(r["company_short"] or "未知", []).append(r)

sample = []
for comp, items in groups.items():
    picked = random.sample(items, min(PER_COMPANY, len(items)))
    for r in picked:
        try:
            sys_fields = json.loads(r["display_fields"]) if r["display_fields"] else {}
        except Exception:
            sys_fields = {}
        sample.append({
            "id": r["id"],
            "company_short": comp,
            "filename": r["filename"],
            "cos_url": r["cos_url"],
            "policy_count": r["policy_count"],
            "policy_index": r["policy_index"],
            "sys_fields": sys_fields,
        })

json.dump(sample, open("/tmp/qc_sample.json", "w"), ensure_ascii=False, indent=1)
print("抽样完成：共 %d 份，覆盖 %d 个保司" % (len(sample), len(groups)))
for comp, items in sorted(groups.items(), key=lambda x: -len(x[1])):
    print("  %-10s 抽 %d / 共 %d" % (comp, min(PER_COMPANY, len(items)), len(items)))
print("已写出 /tmp/qc_sample.json")
