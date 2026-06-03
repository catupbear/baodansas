# -*- coding: utf-8 -*-
"""
一次性数据清理脚本：清除「保险条款注册编号」被旧解析器误识别为身份证号的脏值。

背景：
  旧版 policy_parser 在提取被保险人/投保人身份证号时未限制区域，
  把保单后部「保险条款/释义」附录中的「注册编号」（如 C00013732522023030738143）
  内嵌的 18 位数字串误识别为身份证号。这些脏值还经「同车牌互补」在记录间扩散。
  解析器已修复（以"注册编号"为界、仅取正文，见 2026-06-03-18-26 更新记录），
  本脚本清理存量脏数据。

判定依据：同一身份证号出现在多个不相干车牌/被保险人（含公司主体）上，必为垃圾值。

用法：python3 docs/sql/clean_garbage_id_from_zce.py
幂等：可重复执行。
"""
import yaml
import pymysql
import json

# 已确认的 5 个垃圾身份证值（注册编号误识别衍生）
GARBAGE = {
    "143252202003272567",  # 39 个车牌
    "143231202206281296",  # 18 个车牌
    "143232202206093058",  # 13 个车牌
    "373252202303073814",  # 5 个车牌（紫金驾乘险附加险注册编号）
    "440301196701113473",  # 3 个不相干主体（含公司）
}


def main():
    cfg = yaml.safe_load(open("config.yaml", encoding="utf-8"))
    m = cfg["mysql"]
    conn = pymysql.connect(
        host=m["host"], port=m.get("port", 3306), user=m["user"],
        password=m["password"], database=m["database"],
        charset="utf8mb4", autocommit=False,
    )
    cur = conn.cursor(pymysql.cursors.DictCursor)

    # 1) insurance_records.parsed_fields：任何字段值精确等于垃圾值的置空（raw_text 除外）
    cur.execute("SELECT id, parsed_fields FROM insurance_records WHERE parsed_fields IS NOT NULL")
    rec_fixed = 0
    for r in cur.fetchall():
        try:
            pf = json.loads(r["parsed_fields"])
        except Exception:
            continue
        changed = False
        for k, v in list(pf.items()):
            if k == "raw_text":  # 原文合法包含注册编号，不动
                continue
            if isinstance(v, str) and v in GARBAGE:
                pf[k] = ""
                changed = True
        if changed:
            cur.execute(
                "UPDATE insurance_records SET parsed_fields=%s WHERE id=%s",
                (json.dumps(pf, ensure_ascii=False), r["id"]),
            )
            rec_fixed += 1

    # 2) insurance_policy_fields.id_number（导出扁平表）
    ph = ",".join(["%s"] * len(GARBAGE))
    cur.execute(f"UPDATE insurance_policy_fields SET id_number='' WHERE id_number IN ({ph})", tuple(GARBAGE))
    pf_fixed = cur.rowcount

    conn.commit()
    print(f"清理 insurance_records: {rec_fixed} 条")
    print(f"清理 insurance_policy_fields.id_number: {pf_fixed} 条")
    conn.close()


if __name__ == "__main__":
    main()
