# -*- coding: utf-8 -*-
"""保单识别质检 - PDF 下载脚本（本地跑）。
读 /tmp/qc_sample.json，编码 cos_url 后逐个下载到 /tmp/qc_pdfs/<id>.pdf。
cos_url 是公开 CDN，本地可直接访问；URL 含中文/空格须 quote 编码。"""
import json, os, urllib.request
from urllib.parse import quote

OUT = "/tmp/qc_pdfs"
os.makedirs(OUT, exist_ok=True)
data = json.load(open("/tmp/qc_sample.json"))

ok, fail = 0, 0
for r in data:
    dst = os.path.join(OUT, "%s.pdf" % r["id"])
    try:
        url = quote(r["cos_url"], safe=":/?&=%")
        body = urllib.request.urlopen(url, timeout=40).read()
        if body[:4] != b"%PDF":
            print("  跳过 id=%s（非PDF）" % r["id"]); fail += 1; continue
        open(dst, "wb").write(body)
        ok += 1
    except Exception as e:
        print("  失败 id=%s: %s" % (r["id"], str(e)[:60])); fail += 1

print("下载完成：成功 %d，失败 %d，存于 %s/" % (ok, fail, OUT))
print("待识别清单：")
for r in data:
    p = os.path.join(OUT, "%s.pdf" % r["id"])
    if os.path.exists(p):
        print("  [%s] %s  ->  %s" % (r["company_short"], r["filename"], p))
