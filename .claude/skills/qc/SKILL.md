---
name: qc
description: 保单识别抽样质检。从昨天入库的保单按保司随机抽样，由 Claude 读取 PDF 原件独立识别字段，与系统 parser 提取结果逐项比对，输出准确率差异报告。当用户要"抽查/核对/对账 保单识别准确率""看识别得对不对""质检字段提取"时使用。
---

# 保单识别抽样质检（OCR-QC）

## 目的
独立核对系统对保单字段的识别准确率：Claude 读 **PDF 原件**（不是系统已存的 OCR 文本）自己识别字段，再和系统 parser 提取的结果对比，找出错、漏、多。读原件能同时暴露两类问题：① OCR 没认对 ② parser 规则没提对。

## 环境与凭据
- MySQL 在生产服务器，需 ssh 执行抽样脚本。服务器地址/密码见项目记忆 `project_wxbot_deploy`（root@43.136.92.192，工作目录 /home/wxbot）。
- `cos_url` 是公开 CDN（wxbotcdn.wuhuxiche.com），本地可直接下载 PDF；URL 含中文/空格，下载前必须 `urllib.parse.quote(url, safe=':/?&=%')` 编码。

## 执行步骤

直接调用项目中已有的标准化抽查脚本 `spot-check-skill/scripts/spot_check.py`，它会自动完成抽样、下载 PDF、用 pdfplumber 提取文本、与系统值比对，并生成标准 HTML 报告。

```bash
# 默认：抽查今天和昨天的记录，每保司 10 台车
python3 spot-check-skill/scripts/spot_check.py

# 指定日期范围（如近一个月）
python3 spot-check-skill/scripts/spot_check.py --start 2026-06-01 --end 2026-06-30

# 指定每家保司抽查数量
python3 spot-check-skill/scripts/spot_check.py --start 2026-06-01 --end 2026-06-30 --sample 5
```

用户说"近一个月"时，`--start` 设为 30 天前，`--end` 设为昨天。
用户说"昨天"或不指定范围时，使用默认参数即可。

脚本会：
1. 登录服务器 API 查询指定日期范围内 status=done 的识别记录
2. 按保司分组、按车辆随机抽样（每台车包含所有险种）
3. 下载每条记录的 PDF，用 pdfplumber 提取文本
4. 逐字段比对（保单号、车牌、投保人、被保人、保费、起止期、签单日期等）
5. 生成标准 HTML 报告到 `spot-check-skill/` 目录，包含：
   - 概要统计卡片（总记录数、保司数、抽查数、通过率）
   - 按保司可展开的对比明细（通过率 badge、字段逐项标记 ✓/△/✗）
   - 差异记录高亮、OCR 原文展示、PDF 预览链接

**⚠ 重要：不要手动写 markdown 或自定义 HTML 报告，必须用此脚本生成标准格式。**

脚本执行完后，用 `open` 命令打开生成的 HTML 文件。

## 注意
- **只读不写**，绝不修改数据库或记录。
- 抽样有随机性，每次结果不同。
- 脚本超时约 10 分钟（timeout=600000），365 条记录约需 5-8 分钟。
