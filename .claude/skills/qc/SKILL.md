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

### 1. 抽样（服务器上跑）
把 `scripts/sample.py` scp 到服务器 `/tmp/` 并执行（`cd /home/wxbot && python3 /tmp/sample.py`）。它会：取昨天入库(DATE(created_at)=昨天)、status='done'、未删除、有 cos_url 的记录，按 `company_short` 分组、每组随机抽最多 5 份，写出 `/tmp/qc_sample.json`（每条含 id, company_short, filename, cos_url, policy_count, policy_index, sys_fields=display_fields）。再把 json scp 回本地 `/tmp/qc_sample.json`。

### 2. 下载 PDF（本地跑）
运行 `scripts/download.py`，读 `/tmp/qc_sample.json`，编码 cos_url 逐个下载到 `/tmp/qc_pdfs/<id>.pdf`，打印成功/失败。

### 3. 逐份识别与比对
对每份 PDF：
- 用 **Read** 工具读取 `/tmp/qc_pdfs/<id>.pdf`（只看保单主体页，通常第 1 页；条款/标志页忽略；若 policy_count>1，识别 policy_index 对应的那一份）。
- **以 PDF 上的真实内容为准**，独立识别这些核心字段：
  `保险公司简称、保单号、险种类型、投保人、被保险人、被保险人身份证号码、车牌号、机动车种类、使用性质、发动机号、车架号VIN、厂牌型号、核定载客、排量、初次登记日期、保费合计、不含税保费、增值税额、保险起期、保险止期、收费确认时间`
- 与该记录的 `sys_fields` 逐项比对。比较前先归一化：去空格、统一全半角；日期只比数字（2026-06-25 ≡ 2026年6月25日）；金额只比数值（1100 ≡ 1100.00）；车牌去连字符。分类：
  - 系统空、PDF 有值 → **漏识别**
  - 两者都有但不同 → **识别错误**
  - 系统有值、PDF 确实没有 → **多余/错填**
  - 一致 → 跳过

### 4. 汇总报告（Markdown）
1. **抽样概况**：总份数、覆盖保司数、各保司抽样份数
2. **不一致明细**：表格 `保司 | 文件 | 字段 | PDF真实值 | 系统值 | 类型`
3. **按字段统计**：每个字段出错次数（定位最易错的字段/规则）
4. **按保司统计**：每个保司的字段准确率
5. **结论**：哪些 parser 规则/OCR 环节最该修，给出可落地的修复建议

## 注意
- **只读不写**，绝不修改数据库或记录。
- 抽样有随机性，每次结果不同；要复现可在报告里附上本次抽到的 id 列表。
- 识别要老实：PDF 上没有的字段就标"无"，不要脑补。
