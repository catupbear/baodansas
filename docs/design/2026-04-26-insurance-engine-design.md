# 保单结构化识别引擎 — 设计方案

> 日期：2026-04-26
> 状态：待实施

## 一、背景与目标

当前 `policy_parser.py` 是 1700+ 行的单体文件，所有保司的正则规则混在一起，存在以下问题：

1. **无结构感知** — 每次解析盲试所有正则模式，不知道当前文档的布局模板
2. **无法检测格式变动** — 保司改了模板只有识别失败时才能发现
3. **险种无独立字段定义** — 交强险/商业险/驾意险用同一套提取逻辑，但各险种关键字段差异大
4. **无历史版本兼容** — 保司更新模板后，旧版本保单无法正确识别
5. **OCR 单一来源** — 扫描件仅依赖单一 OCR 引擎，无交叉验证

### 目标

- 每个保司 × 每个险种 × 每个历史版本独立管理，多版本共存
- 全量提取所有可识别字段，按需使用
- 结构变动自动检测 + 告警
- 扫描件多 OCR 引擎交叉验证，关键字段差异标记
- 模板自动学习：上传样本 PDF 自动生成模板和规则，无需人工写正则
- 渐进式迁移，不停机

---

## 二、整体架构

```
PDF 输入
  │
  ▼
┌─────────────────────────────────┐
│  OCR 层                         │
│  pdfplumber 提取文本层           │
│  ├── 有文本层 → 直接用（最准）   │
│  └── 无文本层（扫描件）          │
│       → 并行调多个云 OCR         │
│       → 交叉比对 + 差异标记      │
└──────────┬──────────────────────┘
           ▼
┌─────────────────────────────────┐
│  Stage 1: 保司识别              │  复用现有 4 层策略
│  → company_id = "picc"          │
└──────────┬──────────────────────┘
           ▼
┌─────────────────────────────────┐
│  Stage 2: 险种识别              │  复用现有关键词匹配
│  → policy_type = "compulsory"   │
└──────────┬──────────────────────┘
           ▼
┌─────────────────────────────────┐
│  Stage 3: 模板版本匹配          │  ★ 新增
│  遍历该保司×险种下所有版本       │
│  计算指纹匹配度，选最高分        │
│  → template = "picc/compulsory/v2"
│  → match_score = 0.95           │
└──────────┬──────────────────────┘
           ▼
┌─────────────────────────────────┐
│  Stage 4: 全量字段提取          │  ★ 重构
│  base_rule 提取公共字段          │
│  + 版本专属规则提取险种/保司字段  │
│  → 三层字段结构                  │
│  （扫描件：多 OCR 结果分别提取   │
│    → 逐字段比对 → 投票合并）     │
└──────────┬──────────────────────┘
           ▼
┌─────────────────────────────────┐
│  Stage 5: 结构校验 + 变动检测   │  ★ 新增
│  对比 field_schema 的 required   │
│  检测布局偏移                    │
│  → 正常 / 告警 / 降级            │
└──────────┬──────────────────────┘
           ▼
┌─────────────────────────────────┐
│  Stage 6: 字段映射 + 输出       │  复用 field_mapping
│  三层字段 → 导出列               │
└─────────────────────────────────┘
```

---

## 三、目录结构

```
insurance/
├── engine/                             # 识别引擎
│   ├── __init__.py
│   ├── pipeline.py                     # 主流水线（串联 Stage 1-6）
│   ├── company_identifier.py           # Stage 1：保司识别（从 policy_parser 迁移）
│   ├── type_identifier.py              # Stage 2：险种识别（从 policy_parser 迁移）
│   ├── template_matcher.py             # Stage 3：模板指纹匹配
│   ├── field_extractor.py              # Stage 4：字段提取调度器
│   ├── structure_validator.py          # Stage 5：结构校验 + 变动检测
│   └── schema.py                       # 字段定义、三层结构的数据类
│
├── ocr/                                # OCR 引擎层
│   ├── __init__.py
│   ├── base.py                         # OCR 引擎基类
│   ├── pdfplumber_engine.py            # pdfplumber（从 ocr_service.py 迁移）
│   ├── baidu_engine.py                 # 百度云 OCR（从 baidu_ocr.py 迁移）
│   ├── tencent_engine.py              # 腾讯云 OCR
│   ├── dispatcher.py                   # 多引擎调度器
│   ├── comparator.py                   # 交叉比对引擎
│   └── merger.py                       # 结果合并 + 差异报告
│
├── templates/                          # 模板库
│   ├── registry.json                   # 模板注册表索引
│   ├── picc/                           # 人保
│   │   ├── compulsory/
│   │   │   ├── v1.json
│   │   │   └── v2.json
│   │   ├── commercial/
│   │   │   └── v1.json
│   │   └── accident/
│   │       └── v1.json
│   ├── pingan/                         # 平安
│   │   └── ...
│   └── ...（50+ 保司）
│
├── rules/                              # 解析规则库
│   ├── base_rule.py                    # 公共字段提取（所有保司通用）
│   ├── base_compulsory.py              # 交强险通用字段提取
│   ├── base_commercial.py              # 商业险通用字段提取
│   ├── base_accident.py                # 驾意险通用字段提取
│   ├── picc/
│   │   ├── __init__.py
│   │   ├── compulsory_v1.py
│   │   ├── compulsory_v2.py
│   │   ├── commercial_v1.py
│   │   └── accident_v1.py
│   ├── pingan/
│   │   └── ...
│   └── ...
│
├── alerts/                             # 告警模块
│   ├── __init__.py
│   ├── detector.py                     # 变动检测逻辑
│   ├── notifier.py                     # 告警通知（钉钉/日志）
│   └── alert_records.py               # 告警记录存储
│
├── learner/                            # 模板自动学习引擎
│   ├── __init__.py
│   ├── trainer.py                      # 主入口：接收样本，输出模板+规则
│   ├── text_aligner.py                 # 多文本结构对齐
│   ├── field_discoverer.py             # 字段自动发现
│   ├── pattern_inducer.py              # 正则自动归纳
│   ├── rule_generator.py              # 规则代码生成
│   ├── template_generator.py           # 模板 JSON 生成
│   ├── self_validator.py               # 自验证
│   └── samples/                        # 样本存储（按保司×险种×版本）
│       ├── picc/
│       │   ├── compulsory_v1/
│       │   │   ├── sample_001.txt
│       │   │   ├── sample_002.txt
│       │   │   └── sample_003.txt
│       │   └── ...
│       └── ...
│
├── tools/                              # 模板管理工具
│   ├── template_diff.py                # 两版本模板对比
│   └── batch_verify.py                 # 批量回归验证
│
├── policy_parser.py                    # 保留，迁移期间作为降级入口
├── handler.py                          # 改造：接入新 pipeline
├── ...（其余文件不变）
```

---

## 四、模板版本管理

### 4.1 模板文件结构

每个模板文件（如 `picc/compulsory/v2.json`）：

```json
{
  "id": "picc/compulsory/v2",
  "company_id": "picc",
  "company_name": "中国人民财产保险股份有限公司",
  "company_short": "人保PICC",
  "policy_type": "compulsory",
  "version": "v2",
  "version_label": "2025年改版",
  "effective_date": "2025-01-01",
  "status": "active",
  "created_at": "2026-04-26",

  "fingerprint": {
    "must_have": [
      "机动车交通事故责任强制保险",
      "责任限额"
    ],
    "should_have": [
      "死亡伤残赔偿限额",
      "抢救费用赔偿限额",
      "财产损失赔偿限额"
    ],
    "must_not_have": ["商业保险", "驾乘"],
    "company_signals": ["95518", "PICC", "人民财产"],
    "layout": {
      "sections_order": ["保单号", "投保人", "被保险人", "车辆信息", "责任限额", "保费", "保险期间"],
      "保单号_region": "top_10_lines",
      "保费_after": "责任限额"
    },
    "policy_no_pattern": "^PICC\\d{15,}$"
  },

  "field_schema": {
    "common": {
      "保单号":       {"required": true,  "used": true},
      "投保人":       {"required": false, "used": true},
      "被保险人":     {"required": true,  "used": true},
      "证件号码":     {"required": false, "used": false},
      "车牌号":       {"required": true,  "used": true},
      "车架号VIN":    {"required": false, "used": true},
      "发动机号":     {"required": false, "used": false},
      "厂牌型号":     {"required": false, "used": false},
      "核定载客":     {"required": false, "used": false},
      "核定载质量":   {"required": false, "used": false},
      "使用性质":     {"required": false, "used": false},
      "机动车种类":   {"required": false, "used": false},
      "初次登记日期": {"required": false, "used": false},
      "保费合计":     {"required": true,  "used": true},
      "不含税保费":   {"required": false, "used": false},
      "增值税额":     {"required": false, "used": false},
      "车船税":       {"required": false, "used": true},
      "保险起期":     {"required": true,  "used": true},
      "保险止期":     {"required": true,  "used": true},
      "保险期间":     {"required": false, "used": false},
      "签单日期":     {"required": false, "used": true},
      "投保确认码":   {"required": false, "used": false},
      "收费确认时间": {"required": false, "used": false},
      "销售渠道":     {"required": false, "used": false},
      "中介机构":     {"required": false, "used": false},
      "争议解决方式": {"required": false, "used": false},
      "制单人":       {"required": false, "used": false},
      "经办人":       {"required": false, "used": false}
    },
    "type_specific": {
      "责任限额":         {"required": true,  "used": false},
      "死亡伤残赔偿限额": {"required": true,  "used": false},
      "医疗费用赔偿限额": {"required": false, "used": false},
      "抢救费用赔偿限额": {"required": false, "used": false},
      "财产损失赔偿限额": {"required": true,  "used": false}
    },
    "company_specific": {
      "电子保单验证码":   {"required": false, "used": false},
      "保单查验码":       {"required": false, "used": false}
    }
  },

  "rule_file": "picc/compulsory_v2.py"
}
```

### 4.2 模板注册表（registry.json）

```json
{
  "version": "1.0",
  "updated_at": "2026-04-26",
  "templates": [
    {
      "id": "picc/compulsory/v1",
      "company_id": "picc",
      "policy_type": "compulsory",
      "version": "v1",
      "status": "active",
      "file": "picc/compulsory/v1.json"
    },
    {
      "id": "picc/compulsory/v2",
      "company_id": "picc",
      "policy_type": "compulsory",
      "version": "v2",
      "status": "active",
      "file": "picc/compulsory/v2.json"
    }
  ]
}
```

### 4.3 版本匹配流程

```
文本进入
  → 1. 识别保司（复用现有 4 层策略）
  → 2. 识别险种（交强/商业/驾意）
  → 3. 在该保司×险种下，逐版本计算指纹匹配度
       ├── v2 匹配度 95% → 选中 v2 规则
       ├── v1 匹配度 80% → 备选
       └── 所有版本 < 60% → 告警"疑似新模板"，降级通用规则
  → 4. 用选中版本的专属规则解析
  → 5. 对比 field_schema 检查必填字段是否齐全
       └── 缺失必填字段 → 告警"结构可能变动"
```

---

## 五、全量字段提取 + 分层字段体系

### 5.1 字段分三层

```
第一层：公共字段（所有险种通用）
  ├── 保险公司、保单号、投保人、被保险人、车牌号、车架号VIN ...
  │
第二层：险种专属字段（交强/商业/驾意各自独有）
  ├── 交强险：责任限额、死亡伤残赔偿限额、抢救费用、财产损失赔偿限额 ...
  ├── 商业险：险种明细列表、不计免赔、绝对免赔额、特别约定 ...
  └── 驾意险：保障人数、每人保额、意外身故保额、意外医疗保额 ...
  │
第三层：保司特有字段（某些保司独有的内容）
  └── 平安：平安好车主编码 ...
  └── 人保：电子保单验证码 ...
  └── 太平洋：神行车保产品编号 ...
```

- **`required`**：该字段在此模板中必须出现，缺失则告警
- **`used`**：当前业务是否在用，`false` 表示提取了但暂不参与导出/同步，后续需要时只改配置不改代码

### 5.2 存储结构

`parsed_fields` 改为分层 JSON：

```json
{
  "common": {
    "保单号": "PICC12345",
    "被保险人": "张三",
    "车牌号": "粤B12345",
    "发动机号": "LS5A3CLE8MA123456",
    "厂牌型号": "丰田卡罗拉",
    "...": "（全部公共字段，不管业务用不用都存）"
  },
  "type_specific": {
    "责任限额": "200000",
    "死亡伤残赔偿限额": "180000",
    "抢救费用赔偿限额": "18000",
    "财产损失赔偿限额": "2000"
  },
  "company_specific": {
    "电子保单验证码": "ABCD1234"
  },
  "insurance_items": [
    {"险种名称": "机动车损失保险", "保费": "1200.00", "保额/限额": "150000"}
  ],
  "meta": {
    "template_id": "picc/compulsory/v2",
    "match_score": 0.95,
    "extracted_field_count": 28,
    "required_hit_count": 5,
    "required_total": 5
  }
}
```

---

## 六、规则继承体系

### 6.1 继承链

```
BaseRule（所有保司所有险种的公共字段）
  ├── BaseCompulsoryRule（交强险通用字段）
  │     ├── PiccCompulsoryV1Rule
  │     ├── PiccCompulsoryV2Rule
  │     ├── PinganCompulsoryV1Rule
  │     └── ...
  ├── BaseCommercialRule（商业险通用字段）
  │     ├── PiccCommercialV1Rule
  │     └── ...
  └── BaseAccidentRule（驾意险通用字段）
        ├── PiccAccidentV1Rule
        └── ...
```

### 6.2 BaseRule

```python
# rules/base_rule.py
class BaseRule:
    company_id: str = ""
    policy_type: str = ""
    version: str = ""

    def extract_all(self, text, text_merged) -> dict:
        """主入口：提取三层字段"""
        return {
            "common": self._extract_common(text, text_merged),
            "type_specific": self.extract_type_specific(text, text_merged),
            "company_specific": self.extract_company_specific(text, text_merged),
            "insurance_items": self.extract_items(text, text_merged),
        }

    def _extract_common(self, text, text_merged) -> dict:
        """提取所有公共字段（子类可 override 单个方法）"""
        fields = {}
        fields["保单号"] = self.extract_policy_no(text, text_merged)
        fields["被保险人"] = self.extract_insured(text, text_merged)
        fields["投保人"] = self.extract_proposer(text, text_merged)
        fields.update(self.extract_vehicle_info(text, text_merged))
        fields.update(self.extract_premium(text, text_merged))
        fields.update(self.extract_period(text, text_merged))
        fields["签单日期"] = self.extract_sign_date(text, text_merged)
        fields["投保确认码"] = self.extract_confirm_code(text, text_merged)
        fields["收费确认时间"] = self.extract_fee_confirm_time(text, text_merged)
        fields["销售渠道"] = self.extract_sales_channel(text, text_merged)
        fields["中介机构"] = self.extract_intermediary(text, text_merged)
        fields["争议解决方式"] = self.extract_dispute_resolution(text, text_merged)
        fields["制单人"] = self.extract_underwriter(text, text_merged)
        fields["经办人"] = self.extract_handler(text, text_merged)
        return fields

    # 每个字段一个方法，子类按需 override
    def extract_policy_no(self, text, text_merged): ...
    def extract_insured(self, text, text_merged): ...
    def extract_proposer(self, text, text_merged): ...
    def extract_vehicle_info(self, text, text_merged): ...
    def extract_premium(self, text, text_merged): ...
    def extract_period(self, text, text_merged): ...
    def extract_sign_date(self, text, text_merged): ...
    def extract_type_specific(self, text, text_merged): return {}
    def extract_company_specific(self, text, text_merged): return {}
    def extract_items(self, text, text_merged): return []
```

### 6.3 险种基础规则

```python
# rules/base_compulsory.py — 交强险通用字段
class BaseCompulsoryRule(BaseRule):
    def extract_type_specific(self, text, text_merged):
        fields = {}
        for label in ["死亡伤残赔偿限额", "医疗费用赔偿限额",
                       "抢救费用赔偿限额", "财产损失赔偿限额"]:
            ...
        return fields

# rules/base_commercial.py — 商业险通用字段
class BaseCommercialRule(BaseRule):
    def extract_type_specific(self, text, text_merged):
        """险种明细列表、不计免赔、特别约定"""
        ...
    def extract_items(self, text, text_merged):
        """险种明细行"""
        ...

# rules/base_accident.py — 驾意险通用字段
class BaseAccidentRule(BaseRule):
    def extract_type_specific(self, text, text_merged):
        """保障人数、每人保额、意外身故/医疗保额"""
        ...
```

### 6.4 保司版本规则（示例）

```python
# rules/picc/compulsory_v2.py
"""人保PICC 交强险 v2 版本解析规则（2025年改版）"""

from insurance.rules.base_compulsory import BaseCompulsoryRule

class PiccCompulsoryV2Rule(BaseCompulsoryRule):
    company_id = "picc"
    policy_type = "compulsory"
    version = "v2"

    def extract_policy_no(self, text, text_merged):
        """人保保单号提取 — v2 版本 PICC 开头 + 数字"""
        pattern = r'保险单号[码]?[：:\s]*\s*(PICC\d{15,25})'
        ...

    def extract_insured(self, text, text_merged):
        """人保被保险人提取 — 无特殊格式，复用 base"""
        return super().extract_insured(text, text_merged)

    def extract_type_specific(self, text, text_merged):
        """交强险专属字段：责任限额明细"""
        fields = {}
        for label in ["死亡伤残赔偿限额", "医疗费用赔偿限额",
                       "抢救费用赔偿限额", "财产损失赔偿限额"]:
            pattern = rf'{label}[：:\s]*[￥¥]?([\d,.]+)'
            ...
        return fields

    def extract_company_specific(self, text, text_merged):
        """人保特有字段"""
        fields = {}
        m = re.search(r'验证码[：:\s]*(\w+)', text)
        if m:
            fields["电子保单验证码"] = m.group(1)
        return fields
```

---

## 七、OCR 多引擎交叉验证

### 7.1 核心原则

- **有文本层的 PDF** → 直接用 pdfplumber 提取，文本层是 PDF 内嵌的原始文字，100% 准确，**跳过所有云 OCR**
- **无文本层的 PDF（扫描件）** → 并行调用多个云 OCR，交叉比对差异

### 7.2 调度流程

```
PDF 输入
  │
  ▼
pdfplumber 提取文本层
  │
  ├── 有文本层 → 直接用（最准，零成本，零延迟）
  │
  └── 无文本层（扫描件）
        │
        ▼
      并行调用 百度云 OCR + 腾讯云 OCR（+ 可扩展更多）
        │
        ▼
      交叉比对 + 差异标记
        │
        ▼
      投票合并最优结果
```

### 7.3 OCR 引擎基类

```python
# ocr/base.py
class BaseOCREngine:
    engine_name: str = ""        # "pdfplumber" / "baidu" / "tencent"
    engine_type: str = ""        # "text_layer" / "image"
    priority: int = 0            # 优先级，数字越小优先级越高

    def extract(self, pdf_bytes, page=1) -> OCRResult:
        raise NotImplementedError

@dataclass
class OCRResult:
    engine: str                  # 引擎名
    success: bool
    text: str                    # 提取的原始文本
    confidence: float            # 引擎自身的置信度（0-1）
    cost_ms: int                 # 耗时
    error: str = ""
    page_count: int = 1
    raw_response: dict = None    # 引擎原始返回（调试用）
```

### 7.4 调度器

```python
# ocr/dispatcher.py
class OCRDispatcher:

    def dispatch(self, pdf_bytes, page=1) -> list[OCRResult]:
        # 第一步永远先试 pdfplumber
        text_layer = self._run_engine("pdfplumber", pdf_bytes, page)

        if text_layer.success and text_layer.text.strip():
            # 有文本层 → 直接返回，不需要交叉验证
            return [text_layer]

        # 扫描件 → 并行调所有云 OCR，交叉验证
        cloud_results = self._run_cloud_engines_parallel(pdf_bytes, page)
        return cloud_results

    def _run_cloud_engines_parallel(self, pdf_bytes, page):
        from concurrent.futures import ThreadPoolExecutor, as_completed
        cloud_engines = [e for e in self.engines if e.engine_type == "image"]
        results = []
        with ThreadPoolExecutor(max_workers=len(cloud_engines)) as pool:
            futures = {
                pool.submit(e.extract, pdf_bytes, page): e
                for e in cloud_engines
            }
            for future in as_completed(futures):
                results.append(future.result())
        return results
```

### 7.5 交叉比对引擎

```python
# ocr/comparator.py
class OCRComparator:
    # 关键字段 — 差异必须标记
    CRITICAL_FIELDS = [
        "保单号", "被保险人", "投保人", "车牌号",
        "保费合计", "保险起期", "保险止期"
    ]

    def compare(self, ocr_results: list[OCRResult], rule) -> CompareReport:
        """用同一套规则分别解析每个 OCR 的文本，逐字段对比"""
        # 每个 OCR 结果分别走一遍字段提取
        extractions = {}
        for result in ocr_results:
            if result.success:
                fields = rule.extract_all(result.text, self._merge_text(result.text))
                extractions[result.engine] = fields

        if len(extractions) < 2:
            engine = list(extractions.keys())[0]
            return CompareReport(
                merged_fields=extractions[engine],
                source_engine=engine,
                cross_validated=False, diffs=[])

        # 逐字段对比
        diffs = []
        merged = {}
        for layer in ["common", "type_specific", "company_specific"]:
            merged[layer] = {}
            all_fields = set()
            for eng in extractions:
                all_fields.update(extractions[eng].get(layer, {}).keys())

            for field in all_fields:
                values = {}
                for eng in extractions:
                    val = extractions[eng].get(layer, {}).get(field)
                    if val:
                        values[eng] = val

                if len(set(values.values())) <= 1:
                    merged[layer][field] = list(values.values())[0] if values else ""
                else:
                    diff = FieldDiff(
                        field=field, layer=layer, values=values,
                        is_critical=field in self.CRITICAL_FIELDS,
                        resolved_value=self._vote(field, values, ocr_results),
                        resolution_method="vote")
                    diffs.append(diff)
                    merged[layer][field] = diff.resolved_value

        return CompareReport(
            merged_fields=merged, cross_validated=True,
            diffs=diffs, engine_count=len(extractions))

    def _vote(self, field, values: dict, ocr_results: list) -> str:
        """投票策略：多数一致 → 选多数；无多数 → 按引擎置信度选"""
        val_counts = {}
        for eng, val in values.items():
            val_counts.setdefault(val, []).append(eng)
        majority = max(val_counts.items(), key=lambda x: len(x[1]))
        if len(majority[1]) > len(values) / 2:
            return majority[0]
        best_engine = max(
            values.keys(),
            key=lambda eng: next(r.confidence for r in ocr_results if r.engine == eng))
        return values[best_engine]

@dataclass
class FieldDiff:
    field: str                   # 字段名
    layer: str                   # common/type_specific/company_specific
    values: dict                 # {"baidu": "张三", "tencent": "张二"}
    is_critical: bool            # 是否关键字段
    resolved_value: str          # 最终采用的值
    resolution_method: str       # "vote" / "priority"
```

### 7.6 OCR 配置

存入 `insurance_config` 表，前端可配：

```json
{
  "config_key": "ocr_settings",
  "config_value": {
    "engines": {
      "pdfplumber": {"enabled": true},
      "baidu": {"enabled": true, "api_key": "...", "secret_key": "..."},
      "tencent": {"enabled": true, "secret_id": "...", "secret_key": "..."}
    },
    "critical_diff_alert": true
  }
}
```

---

## 八、模板匹配引擎

```python
# engine/template_matcher.py
class TemplateMatcher:
    def __init__(self, templates_dir):
        self.registry = self._load_registry(templates_dir)
        self.templates = self._load_all_templates()

    def match(self, text, company_id, policy_type) -> MatchResult:
        candidates = self._get_candidates(company_id, policy_type)
        if not candidates:
            return MatchResult(template=None, score=0, status="no_template")

        results = []
        for tpl in candidates:
            score = self._calc_score(text, tpl)
            results.append((tpl, score))
        results.sort(key=lambda x: x[1], reverse=True)
        best_tpl, best_score = results[0]

        if best_score >= 0.8:
            return MatchResult(template=best_tpl, score=best_score, status="matched")
        elif best_score >= 0.5:
            return MatchResult(template=best_tpl, score=best_score,
                             status="partial_match",
                             warning="模板匹配度偏低，可能存在结构变动")
        else:
            return MatchResult(template=None, score=best_score,
                             status="no_match", warning="疑似新模板版本")

    def _calc_score(self, text, template) -> float:
        fp = template["fingerprint"]
        score = 0
        total_weight = 0

        # must_have 权重最高（每个 0.2）
        for kw in fp["must_have"]:
            total_weight += 0.2
            if kw in text:
                score += 0.2

        # must_not_have 命中则直接扣分
        for kw in fp["must_not_have"]:
            if kw in text:
                score -= 0.3

        # should_have（每个 0.1）
        for kw in fp["should_have"]:
            total_weight += 0.1
            if kw in text:
                score += 0.1

        # company_signals（每个 0.05）
        for signal in fp["company_signals"]:
            total_weight += 0.05
            if signal in text:
                score += 0.05

        # 保单号格式匹配（0.15）
        if "policy_no_pattern" in fp:
            total_weight += 0.15
            ...

        return max(0, score / total_weight) if total_weight > 0 else 0
```

---

## 九、结构变动检测 + 告警

### 9.1 校验逻辑

```python
# engine/structure_validator.py
class StructureValidator:

    def validate(self, extracted_fields, template, match_score) -> ValidationResult:
        alerts = []
        schema = template["field_schema"]

        # 1. 必填字段缺失检查
        missing_required = []
        for layer in ["common", "type_specific", "company_specific"]:
            for field, config in schema.get(layer, {}).items():
                if config["required"]:
                    value = extracted_fields.get(layer, {}).get(field)
                    if not value:
                        missing_required.append(field)

        if missing_required:
            alerts.append(Alert(
                level="warning", type="missing_required_fields",
                message=f"必填字段缺失: {missing_required}",
                template_id=template["id"]))

        # 2. 模板匹配度偏低
        if match_score < 0.8:
            alerts.append(Alert(
                level="warning", type="low_match_score",
                message=f"模板匹配度 {match_score:.0%}，可能存在结构变动",
                template_id=template["id"]))

        # 3. 布局偏移检测
        layout_drift = self._check_layout_drift(
            extracted_fields, template["fingerprint"].get("layout", {}))
        if layout_drift:
            alerts.append(Alert(
                level="info", type="layout_drift",
                message=f"布局偏移: {layout_drift}"))

        # 4. 新字段出现
        unknown = self._detect_unknown_fields(extracted_fields, schema)
        if unknown:
            alerts.append(Alert(
                level="info", type="new_fields_detected",
                message=f"发现未定义字段: {unknown}"))

        return ValidationResult(is_valid=len(missing_required) == 0, alerts=alerts)
```

### 9.2 告警通知

```python
# alerts/notifier.py
class AlertNotifier:
    def notify(self, alert, context):
        # 记录到数据库
        self._save_to_db(alert, context)
        # warning 级别以上推送钉钉群通知
        if alert.level in ("warning", "critical"):
            self._send_dingtalk(alert, context)

    def _send_dingtalk(self, alert, context):
        message = (
            f"保单结构变动告警\n"
            f"保司: {context['company_short']}\n"
            f"险种: {context['policy_type']}\n"
            f"模板: {alert.template_id}\n"
            f"问题: {alert.message}\n"
            f"文件: {context['filename']}")
        ...
```

---

## 十、主流水线

```python
# engine/pipeline.py
class InsurancePipeline:
    def __init__(self):
        self.company_identifier = CompanyIdentifier()
        self.type_identifier = TypeIdentifier()
        self.template_matcher = TemplateMatcher("insurance/templates")
        self.field_extractor = FieldExtractor("insurance/rules")
        self.structure_validator = StructureValidator()
        self.alert_notifier = AlertNotifier()
        self.ocr_dispatcher = OCRDispatcher(engines=[...], config=...)
        self.ocr_comparator = OCRComparator()

    def process_from_pdf(self, pdf_bytes, filename="") -> PipelineResult:
        # OCR 阶段
        ocr_results = self.ocr_dispatcher.dispatch(pdf_bytes)

        # 用第一个成功的文本做保司/险种/模板识别
        primary_text = self._get_primary_text(ocr_results)

        # Stage 1: 识别保司
        company = self.company_identifier.identify(primary_text)

        # Stage 2: 识别险种
        policy_type = self.type_identifier.identify(primary_text)

        # Stage 3: 匹配模板版本
        match = self.template_matcher.match(
            primary_text, company["company_id"], policy_type["type_code"])

        # 加载规则
        if match.template:
            rule = self.field_extractor.load_rule(match.template["rule_file"])
        else:
            rule = self.field_extractor.load_base_rule(policy_type["type_code"])

        # Stage 4: 字段提取
        is_scan = len(ocr_results) > 1  # 多个 OCR 结果说明是扫描件
        if is_scan:
            # 扫描件：交叉比对
            compare_report = self.ocr_comparator.compare(ocr_results, rule)
            extracted = compare_report.merged_fields
            ocr_diffs = compare_report.diffs
            if compare_report.has_critical_diff:
                self.alert_notifier.notify(Alert(
                    level="warning", type="ocr_critical_diff",
                    message=self._format_diff_message(compare_report.diffs)),
                    {"company_short": company["company_short"],
                     "policy_type": policy_type["type_name"],
                     "filename": filename})
        else:
            # 文本层 PDF：直接提取
            text = ocr_results[0].text
            extracted = rule.extract_all(text, self._merge_text(text))
            ocr_diffs = []

        # Stage 5: 结构校验
        if match.template:
            validation = self.structure_validator.validate(
                extracted, match.template, match.score)
            for alert in validation.alerts:
                self.alert_notifier.notify(alert, {
                    "company_short": company["company_short"],
                    "policy_type": policy_type["type_name"],
                    "filename": filename})
        else:
            validation = ValidationResult(is_valid=True, alerts=[])

        # Stage 6: 字段映射
        mapped = apply_mapping(extracted, company["company_short"])

        return PipelineResult(
            company=company,
            policy_type=policy_type,
            template_id=match.template["id"] if match.template else None,
            match_score=match.score,
            fields=extracted,
            mapped_fields=mapped,
            validation=validation,
            ocr_diffs=ocr_diffs,
            doc_category=self._identify_doc_category(primary_text, extracted))
```

---

## 十一、数据库变更

### 新增表：template_alert_records

```sql
CREATE TABLE template_alert_records (
    id INT PRIMARY KEY AUTO_INCREMENT,
    insurance_record_id INT,
    template_id VARCHAR(128) COMMENT '匹配的模板ID',
    match_score FLOAT COMMENT '匹配度',
    alert_level VARCHAR(16) COMMENT 'info/warning/critical',
    alert_type VARCHAR(64) COMMENT 'missing_required_fields/low_match_score/ocr_critical_diff/...',
    alert_message TEXT,
    resolved TINYINT DEFAULT 0 COMMENT '是否已处理',
    resolved_action VARCHAR(32) COMMENT 'new_version/ignore/fix_rule',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_template (template_id),
    INDEX idx_level (alert_level),
    INDEX idx_resolved (resolved)
);
```

### insurance_records 表新增字段

```sql
ALTER TABLE insurance_records ADD COLUMN template_id VARCHAR(128) COMMENT '匹配的模板版本';
ALTER TABLE insurance_records ADD COLUMN match_score FLOAT COMMENT '模板匹配度';
ALTER TABLE insurance_records ADD COLUMN field_layer_data LONGTEXT COMMENT '三层结构化字段JSON';
ALTER TABLE insurance_records ADD COLUMN ocr_engines VARCHAR(128) COMMENT '使用的OCR引擎列表';
ALTER TABLE insurance_records ADD COLUMN ocr_cross_validated TINYINT DEFAULT 0 COMMENT '是否经过交叉验证';
ALTER TABLE insurance_records ADD COLUMN ocr_diffs LONGTEXT COMMENT 'OCR差异详情JSON';
ALTER TABLE insurance_records ADD COLUMN has_critical_diff TINYINT DEFAULT 0 COMMENT '是否有关键字段差异';
```

---

## 十二、模板自动学习引擎

### 12.1 核心原则

**不需要人工写正则**。上传 N 份同类保单 PDF（建议 3-5 份），系统自动：
1. 对比多份文本，找出固定部分（标签）和变化部分（值）
2. 从变化部分归纳提取正则
3. 生成模板 JSON + 规则 .py 文件
4. 用所有样本反跑验证，通过后自动注册

### 12.2 学习流程

```
上传 N 份同类保单 PDF（建议 3-5 份）
  │
  ▼
┌────────────────────────────────────┐
│  Step 1: 文本提取                   │
│  每份 PDF → pdfplumber 提取文本     │
│  → 得到 N 份原始文本                │
└──────────┬─────────────────────────┘
           ▼
┌────────────────────────────────────┐
│  Step 2: 结构对齐                   │
│  N 份文本按行对齐，找出：            │
│  - 固定文本（每份都一样的行）        │  → 指纹关键词
│  - 变量文本（每份不同的值）          │  → 字段位置
│  - 固定标签 + 变量值 的配对          │  → 提取规则
└──────────┬─────────────────────────┘
           ▼
┌────────────────────────────────────┐
│  Step 3: 字段自动发现               │
│  识别「标签：值」模式                │
│  标签固定 + 值在样本间变化 → 字段    │
│  自动归类到 common / type_specific  │
└──────────┬─────────────────────────┘
           ▼
┌────────────────────────────────────┐
│  Step 4: 正则自动归纳               │
│  对每个字段的 N 个值，归纳正则模式   │
│  "张三" / "李四" / "王五"           │
│  → [\u4e00-\u9fa5]{2,4}（2-4个汉字）│
└──────────┬─────────────────────────┘
           ▼
┌────────────────────────────────────┐
│  Step 5: 生成模板 + 规则            │
│  → template JSON（指纹 + schema）   │
│  → rule .py（提取正则）             │
└──────────┬─────────────────────────┘
           ▼
┌────────────────────────────────────┐
│  Step 6: 自验证                     │
│  用生成的规则反跑所有样本            │
│  检查提取率 + 一致性                │
│  → 通过（≥90%）→ 自动注册           │
│  → 不通过 → 标记需人工确认的字段     │
└────────────────────────────────────┘
```

### 12.3 文本结构对齐（text_aligner.py）

将 N 份同类保单文本按行对齐，区分三种行类型：

```python
class TextAligner:
    """将 N 份同类保单文本按行对齐，区分固定区域和变量区域"""

    def align(self, texts: list[str]) -> AlignmentResult:
        line_groups = [text.split('\n') for text in texts]

        aligned_lines = []
        for line_idx in range(max(len(g) for g in line_groups)):
            lines_at_idx = []
            for group in line_groups:
                if line_idx < len(group):
                    lines_at_idx.append(group[line_idx].strip())
                else:
                    lines_at_idx.append("")

            unique_lines = set(l for l in lines_at_idx if l)
            if len(unique_lines) == 1:
                # 所有样本该行完全一致 → 固定文本（模板指纹）
                aligned_lines.append(AlignedLine(
                    index=line_idx, type="fixed",
                    content=unique_lines.pop(), variants=[]))
            elif len(unique_lines) > 1:
                # 有差异 → 分析是整行变化还是部分变化
                common_prefix, common_suffix = self._find_common_affixes(lines_at_idx)
                if common_prefix or common_suffix:
                    # 有公共前后缀 → 「标签：值」模式
                    values = [self._extract_variable_part(
                        l, common_prefix, common_suffix) for l in lines_at_idx]
                    aligned_lines.append(AlignedLine(
                        index=line_idx, type="label_value",
                        content=common_prefix,
                        label=common_prefix.rstrip('：: '),
                        variants=values))
                else:
                    # 整行都不同 → 纯变量行
                    aligned_lines.append(AlignedLine(
                        index=line_idx, type="variable",
                        content="", variants=list(unique_lines)))

        return AlignmentResult(lines=aligned_lines, sample_count=len(texts))

    def _find_common_affixes(self, lines: list[str]) -> tuple[str, str]:
        """找多行文本的公共前缀和后缀"""
        non_empty = [l for l in lines if l]
        if not non_empty:
            return "", ""
        prefix = os.path.commonprefix(non_empty)
        reversed_lines = [l[::-1] for l in non_empty]
        suffix = os.path.commonprefix(reversed_lines)[::-1]
        return prefix, suffix
```

**对齐示例**：

```
样本1: "保险单号：PICC12345678901234"
样本2: "保险单号：PICC98765432109876"
样本3: "保险单号：PICC55566677788899"
→ type="label_value", label="保险单号", variants=["PICC123...", "PICC987...", "PICC555..."]

样本1: "机动车交通事故责任强制保险单"
样本2: "机动车交通事故责任强制保险单"
样本3: "机动车交通事故责任强制保险单"
→ type="fixed", content="机动车交通事故责任强制保险单"
```

### 12.4 字段自动发现（field_discoverer.py）

```python
class FieldDiscoverer:
    """从对齐结果中自动发现字段"""

    # 已知字段标签 → 标准字段名映射
    KNOWN_LABELS = {
        "保险单号": "保单号", "保单号": "保单号", "保单号码": "保单号",
        "被保险人": "被保险人", "被保险人名称": "被保险人",
        "投保人": "投保人", "投保人名称": "投保人",
        "号牌号码": "车牌号", "车牌号": "车牌号",
        "车架号": "车架号VIN", "车辆识别代号": "车架号VIN",
        "发动机号": "发动机号", "发动机号码": "发动机号",
        "厂牌型号": "厂牌型号", "品牌型号": "厂牌型号",
        "保险费合计": "保费合计", "总保险费": "保费合计",
        "签单日期": "签单日期", "签发日期": "签单日期",
        # ... 更多映射（覆盖各保司对同一字段的不同叫法）
    }

    # 字段 → 所属层级
    FIELD_LAYER = {
        "保单号": "common", "被保险人": "common", "投保人": "common",
        "车牌号": "common", "保费合计": "common", "保险起期": "common",
        "保险止期": "common", "签单日期": "common", "车架号VIN": "common",
        "发动机号": "common", "厂牌型号": "common", "车船税": "common",
        "责任限额": "type_specific",
        "死亡伤残赔偿限额": "type_specific",
        "抢救费用赔偿限额": "type_specific",
        "财产损失赔偿限额": "type_specific",
        # 不在此表中的 → company_specific
    }

    def discover(self, alignment: AlignmentResult) -> list[DiscoveredField]:
        fields = []

        for line in alignment.lines:
            if line.type == "label_value":
                # 有明确的标签：值对
                field = self._match_known_label(line.label)
                if field:
                    fields.append(DiscoveredField(
                        standard_name=field["standard_name"],
                        layer=field["layer"],
                        original_label=line.label,
                        line_index=line.index,
                        sample_values=line.variants,
                        source="label_match",
                        confidence=1.0))
                else:
                    # 未知标签 → 也记录，归入 company_specific
                    fields.append(DiscoveredField(
                        standard_name=line.label,
                        layer="company_specific",
                        original_label=line.label,
                        line_index=line.index,
                        sample_values=line.variants,
                        source="auto_discover",
                        confidence=0.7))

            elif line.type == "variable":
                # 纯变量行 → 尝试用上下文推断
                context_field = self._infer_from_context(line, alignment.lines)
                if context_field:
                    fields.append(context_field)

        # 检测多行联合字段（如保险期间跨两行）
        fields.extend(self._discover_multi_line_fields(alignment))

        # 检测表格结构（如商业险险种明细）
        fields.extend(self._discover_table_fields(alignment))

        return fields

    def _discover_table_fields(self, alignment) -> list[DiscoveredField]:
        """发现表格结构（如险种明细列表）
        找连续的、列数相同的行 → 可能是表格
        第一行是表头（固定文本），后续行是数据（变量）
        """
        ...
```

### 12.5 正则自动归纳（pattern_inducer.py）

```python
class PatternInducer:
    """从多个值样本自动归纳正则表达式"""

    # 字段类型 → 预定义模式
    FIELD_TYPE_PATTERNS = {
        "保单号": {
            "type": "alphanumeric",
            "min_len": 15, "max_len": 30
        },
        "被保险人": {
            "type": "chinese_name",
            "min_len": 2, "max_len": 20
        },
        "车牌号": {
            "type": "plate",
            "pattern": r'[京津沪渝冀豫云辽黑湘皖鲁新苏浙赣鄂桂甘晋蒙陕吉闽贵粤川青藏琼宁][A-Z][A-Z0-9]{5,6}'
        },
        "保费合计": {
            "type": "money",
            "pattern": r'[\d,]+\.?\d*'
        },
    }

    def induce(self, field: DiscoveredField) -> InducedPattern:
        """根据字段名和样本值，归纳提取正则"""
        values = [v for v in field.sample_values if v]

        # 1. 先检查是否有预定义模式
        if field.standard_name in self.FIELD_TYPE_PATTERNS:
            preset = self.FIELD_TYPE_PATTERNS[field.standard_name]
            if "pattern" in preset:
                if all(re.search(preset["pattern"], v) for v in values):
                    return InducedPattern(
                        field=field.standard_name,
                        label_pattern=self._build_label_regex(field.original_label),
                        value_pattern=preset["pattern"],
                        full_pattern=self._combine(field.original_label, preset["pattern"]),
                        source="preset", confidence=0.95)

        # 2. 自动分析值的模式
        value_pattern = self._analyze_values(values)

        # 3. 构建完整提取正则：标签 + 分隔符 + 值
        label_pattern = self._build_label_regex(field.original_label)
        separator = r'[：:\s]*\s*'
        full_pattern = f'{label_pattern}{separator}({value_pattern})'

        return InducedPattern(
            field=field.standard_name,
            label_pattern=label_pattern,
            value_pattern=value_pattern,
            full_pattern=full_pattern,
            source="induced",
            confidence=self._calc_confidence(values, value_pattern))

    def _analyze_values(self, values: list[str]) -> str:
        """分析多个值的共性，归纳正则"""
        if not values:
            return r'\S+'

        # 纯数字（可能有逗号和小数点）
        if all(re.match(r'^[\d,.]+$', v) for v in values):
            return r'[\d,]+\.?\d*'

        # 纯中文
        if all(re.match(r'^[\u4e00-\u9fa5]+$', v) for v in values):
            lengths = [len(v) for v in values]
            return rf'[\u4e00-\u9fa5]{{{min(lengths)},{max(lengths)}}}'

        # 纯字母数字
        if all(re.match(r'^[A-Za-z0-9]+$', v) for v in values):
            lengths = [len(v) for v in values]
            return rf'[A-Za-z0-9]{{{min(lengths)},{max(lengths)}}}'

        # 日期格式
        if all(re.match(r'^\d{4}[年-]\d{1,2}[月-]\d{1,2}', v) for v in values):
            return r'\d{4}[年-]\d{1,2}[月-]\d{1,2}[日]?'

        # 混合类型 → 通用模式
        return r'\S+'

    def _build_label_regex(self, label: str) -> str:
        """为标签生成容错正则（处理 OCR 可能的空格插入）
        "被保险人" → "被\\s*保\\s*险\\s*人"
        """
        chars = list(label)
        return r'\s*'.join(re.escape(c) for c in chars)
```

### 12.6 规则代码生成（rule_generator.py）

```python
class RuleGenerator:
    """根据发现的字段和归纳的正则，生成规则 Python 文件"""

    def generate(self, company_id: str, policy_type: str, version: str,
                 fields: list[DiscoveredField],
                 patterns: list[InducedPattern]) -> str:

        # 选择基类
        base_class_map = {
            "compulsory": ("BaseCompulsoryRule", "base_compulsory"),
            "commercial": ("BaseCommercialRule", "base_commercial"),
            "accident":   ("BaseAccidentRule", "base_accident"),
        }
        base_class, base_module = base_class_map.get(
            policy_type, ("BaseRule", "base_rule"))

        class_name = (f"{self._to_class_name(company_id)}"
                      f"{policy_type.capitalize()}"
                      f"{version.upper()}Rule")

        # 按字段层级分组
        common_patterns = [p for p in patterns
                          if self._get_layer(p.field) == "common"]
        type_patterns = [p for p in patterns
                        if self._get_layer(p.field) == "type_specific"]
        company_patterns = [p for p in patterns
                           if self._get_layer(p.field) == "company_specific"]

        code = f'''"""
{company_id} {policy_type} {version} 解析规则
自动生成于 {datetime.now().strftime("%Y-%m-%d %H:%M")}
样本数量: {len(fields[0].sample_values) if fields else 0}
"""
import re
from insurance.rules.{base_module} import {base_class}


class {class_name}({base_class}):
    company_id = "{company_id}"
    policy_type = "{policy_type}"
    version = "{version}"
'''

        # 生成每个需要 override 的公共字段提取方法
        for pattern in common_patterns:
            method_name = self._field_to_method(pattern.field)
            code += f'''
    def {method_name}(self, text, text_merged):
        """{pattern.field}提取 — 自动归纳"""
        m = re.search(r\'{pattern.full_pattern}\', text)
        if m:
            return m.group(1).strip()
        m = re.search(r\'{pattern.full_pattern}\', text_merged)
        if m:
            return m.group(1).strip()
        return super().{method_name}(text, text_merged)
'''

        # 生成 type_specific 提取方法
        if type_patterns:
            code += '''
    def extract_type_specific(self, text, text_merged):
        """险种专属字段 — 自动归纳"""
        fields = super().extract_type_specific(text, text_merged)
'''
            for pattern in type_patterns:
                code += f'''
        m = re.search(r\'{pattern.full_pattern}\', text)
        if m:
            fields["{pattern.field}"] = m.group(1).strip()
'''
            code += '        return fields\n'

        # 生成 company_specific 提取方法
        if company_patterns:
            code += '''
    def extract_company_specific(self, text, text_merged):
        """保司特有字段 — 自动归纳"""
        fields = {}
'''
            for pattern in company_patterns:
                code += f'''
        m = re.search(r\'{pattern.full_pattern}\', text)
        if m:
            fields["{pattern.field}"] = m.group(1).strip()
'''
            code += '        return fields\n'

        return code
```

### 12.7 自验证（self_validator.py）

```python
class SelfValidator:
    """用生成的规则反跑所有样本，验证准确率"""

    def validate(self, rule, samples: list[str],
                 discovered_fields: list[DiscoveredField]) -> ValidationReport:

        results = []
        for i, text in enumerate(samples):
            text_merged = self._merge_text(text)
            extracted = rule.extract_all(text, text_merged)

            # 和已知样本值对比
            field_results = []
            for field in discovered_fields:
                expected = field.sample_values[i] if i < len(field.sample_values) else None
                actual = self._get_field_value(extracted, field)

                if expected and actual:
                    match = self._fuzzy_match(expected, actual)
                    field_results.append(FieldValidation(
                        field=field.standard_name,
                        expected=expected, actual=actual, match=match))
                elif expected and not actual:
                    field_results.append(FieldValidation(
                        field=field.standard_name,
                        expected=expected, actual="", match=False))

            results.append(SampleValidation(
                sample_index=i, fields=field_results,
                hit_rate=sum(1 for f in field_results if f.match) / len(field_results)
                         if field_results else 0))

        overall_hit_rate = (
            sum(r.hit_rate for r in results) / len(results) if results else 0)

        # 找出哪些字段提取不稳定
        unstable_fields = []
        for field in discovered_fields:
            field_matches = [
                r for sample in results
                for r in sample.fields
                if r.field == field.standard_name]
            fail_count = sum(1 for r in field_matches if not r.match)
            if fail_count > 0:
                unstable_fields.append({
                    "field": field.standard_name,
                    "fail_count": fail_count,
                    "total": len(field_matches),
                    "needs_manual_review": True})

        return ValidationReport(
            sample_count=len(samples),
            overall_hit_rate=overall_hit_rate,
            passed=overall_hit_rate >= 0.9 and len(unstable_fields) == 0,
            unstable_fields=unstable_fields,
            details=results)
```

### 12.8 训练器主入口（trainer.py）

```python
class TemplateTrainer:
    """主入口：接收样本 PDF，输出模板 + 规则"""

    def __init__(self):
        self.aligner = TextAligner()
        self.discoverer = FieldDiscoverer()
        self.inducer = PatternInducer()
        self.rule_gen = RuleGenerator()
        self.template_gen = TemplateGenerator()
        self.validator = SelfValidator()

    def train(self, pdf_bytes_list: list[bytes],
              company_id: str, policy_type: str,
              version: str = "v1") -> TrainResult:
        """
        输入：N 份同类保单 PDF + 保司ID + 险种
        输出：模板 JSON + 规则 .py + 验证报告
        """

        # Step 1: 提取文本
        texts = []
        for pdf_bytes in pdf_bytes_list:
            result = extract_text_from_pdf_bytes(pdf_bytes)
            if result["success"]:
                texts.append(result["text"])
        if len(texts) < 2:
            return TrainResult(success=False, error="至少需要2份有效样本")

        # Step 2: 结构对齐
        alignment = self.aligner.align(texts)

        # Step 3: 字段发现
        fields = self.discoverer.discover(alignment)

        # Step 4: 正则归纳
        patterns = []
        for field in fields:
            pattern = self.inducer.induce(field)
            patterns.append(pattern)

        # Step 5: 生成模板 + 规则
        template_json = self.template_gen.generate(
            company_id, policy_type, version,
            fields, patterns, alignment)
        rule_code = self.rule_gen.generate(
            company_id, policy_type, version,
            fields, patterns)

        # Step 6: 自验证
        rule_instance = self._load_rule_from_code(rule_code)
        validation = self.validator.validate(rule_instance, texts, fields)

        # 保存样本文本（用于后续版本对比）
        self._save_samples(texts, company_id, policy_type, version)

        return TrainResult(
            success=validation.passed,
            template=template_json,
            rule_code=rule_code,
            validation=validation,
            auto_registered=validation.passed,
            needs_review=validation.unstable_fields)
```

### 12.9 训练 API 接口

```python
# insurance/api.py 新增

@insurance_bp.route('/api/insurance/train', methods=['POST'])
def train_template():
    """上传样本 PDF，自动训练模板"""
    files = request.files.getlist('samples')
    company_id = request.form.get('company_id')      # 如 "picc"
    policy_type = request.form.get('policy_type')     # 如 "compulsory"
    version = request.form.get('version', 'v1')

    pdf_bytes_list = [f.read() for f in files]

    trainer = TemplateTrainer()
    result = trainer.train(pdf_bytes_list, company_id, policy_type, version)

    if result.success:
        # 自动写入模板文件和规则文件
        _save_template(result.template, company_id, policy_type, version)
        _save_rule(result.rule_code, company_id, policy_type, version)
        _update_registry(company_id, policy_type, version)

    return jsonify({
        "success": result.success,
        "validation": {
            "hit_rate": result.validation.overall_hit_rate,
            "sample_count": result.validation.sample_count,
            "unstable_fields": result.validation.unstable_fields
        },
        "needs_review": result.needs_review,
        "template_id": f"{company_id}/{policy_type}/{version}"
    })
```

### 12.10 前端训练界面

```
┌─────────────────────────────────────────────┐
│  模板训练                                    │
│                                             │
│  保司：[下拉选择 / 新增]  险种：[交强/商业/驾意]│
│  版本：[自动递增]                             │
│                                             │
│  ┌─────────────────────────────────┐        │
│  │  拖拽上传样本 PDF（3-5份同类保单）│        │
│  │  📄 人保交强险_样本1.pdf         │        │
│  │  📄 人保交强险_样本2.pdf         │        │
│  │  📄 人保交强险_样本3.pdf         │        │
│  └─────────────────────────────────┘        │
│                                             │
│  [开始训练]                                  │
│                                             │
│  ─── 训练结果 ───                            │
│  整体准确率: 96.7%  ✅ 通过                   │
│  样本数量: 3                                 │
│  发现字段: 28                                │
│                                             │
│  ⚠️ 需人工确认的字段:                         │
│  ┌──────────┬────────┬────────┬──────┐      │
│  │ 字段      │ 失败数  │ 总数   │ 操作  │     │
│  ├──────────┼────────┼────────┼──────┤      │
│  │ 发动机号   │ 1      │ 3     │ [查看] │    │
│  └──────────┴────────┴────────┴──────┘      │
│                                             │
│  [确认注册模板]  [重新训练]  [导出规则预览]     │
└─────────────────────────────────────────────┘
```

### 12.11 版本变动自动发现 + 自动训练

当线上识别触发告警并积累足够样本时，系统可自动发起训练：

```
线上保单匹配度低（< 0.6）
       │
       ▼
  系统自动收集这些低匹配保单的文本
       │
       ▼
  同一保司×险种积累到 ≥ 3 份
       │
       ▼
  自动调用 trainer.train() 生成新版本候选
       │
       ▼
  推送通知："检测到人保交强险疑似新版本，
            已自动生成 v3 模板，准确率 93%，
            请确认是否注册"
       │
       ▼
  人工点击 [确认] → 新版本上线
  旧版本保持 active（历史兼容）
```

### 12.12 新增数据库表：template_train_records

```sql
CREATE TABLE template_train_records (
    id INT PRIMARY KEY AUTO_INCREMENT,
    company_id VARCHAR(64) COMMENT '保司ID',
    policy_type VARCHAR(32) COMMENT '险种',
    version VARCHAR(16) COMMENT '版本号',
    sample_count INT COMMENT '样本数量',
    discovered_fields INT COMMENT '发现字段数',
    overall_hit_rate FLOAT COMMENT '整体准确率',
    passed TINYINT COMMENT '是否通过验证',
    unstable_fields LONGTEXT COMMENT '不稳定字段JSON',
    template_json LONGTEXT COMMENT '生成的模板JSON',
    rule_code LONGTEXT COMMENT '生成的规则代码',
    registered TINYINT DEFAULT 0 COMMENT '是否已注册',
    trigger_source VARCHAR(32) COMMENT 'manual/auto（手动训练/自动触发）',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_company_type (company_id, policy_type),
    INDEX idx_registered (registered)
);
```

---

## 十三、模板版本生命周期

```
新保单识别失败 / 匹配度低
       │
       ▼
  告警推送（钉钉 + 数据库记录）
       │
       ▼
  系统自动收集低匹配保单
       │
       ▼
  同一保司×险种积累 ≥ 3 份
       │
       ▼
  自动调用 TemplateTrainer.train()
  → 自动生成模板 + 规则 + 验证报告
       │
       ▼
  推送通知（含准确率和不稳定字段）
       │
       ▼
  人工确认
       │
   ┌───┴───────────────┐
   确认注册              需要调整
   │                    │
   ▼                    ▼
  新版本上线             上传更多样本 / 人工微调规则
  旧版本保持 active      重新训练
  （历史兼容）
```

---

## 十四、迁移阶段

```
Phase 1（基础框架）
  ├── 搭建 engine/、ocr/、templates/、rules/、learner/ 目录结构
  ├── 实现 BaseRule + 三个险种基础规则
  ├── 实现 TemplateMatcher + StructureValidator
  ├── pipeline.py 串联所有 Stage
  └── 数据库表变更（insurance_records 新字段 + template_alert_records + template_train_records）

Phase 2（OCR 引擎层重构）
  ├── ocr/ 目录，统一引擎接口
  ├── 迁移 pdfplumber + 百度 OCR 到新接口
  ├── 新增腾讯云 OCR 引擎
  ├── 实现 dispatcher + comparator + merger
  └── 交叉验证集成到 pipeline

Phase 3（自动学习引擎）
  ├── 实现 text_aligner、field_discoverer、pattern_inducer
  ├── 实现 rule_generator、template_generator、self_validator
  ├── 实现 trainer 主入口
  ├── 训练 API 接口 + 前端训练界面
  └── 用现有保单样本验证学习效果

Phase 4（规则迁移 — 用自动学习批量生成）
  ├── 收集每个保司×险种的 3-5 份历史样本
  ├── 批量调用 trainer.train() 自动生成模板和规则
  ├── 人工确认通过率，处理不稳定字段
  ├── 与旧 policy_parser.py 结果对比验证
  └── 逐个保司切换到新规则

Phase 5（handler 切换）
  ├── handler.py 接入新 pipeline
  ├── 旧 policy_parser.py 作为降级后备
  └── match_score < 0.5 时自动降级到旧逻辑

Phase 6（告警 + 自动发现）
  ├── 告警通知接入钉钉
  ├── 前端模板管理 + 训练管理页面
  ├── 低匹配保单自动收集 + 自动触发训练
  └── 批量回归验证工具

Phase 7（清理）
  ├── 确认所有保司迁移完成
  ├── 删除旧 policy_parser.py
  └── 文档更新
```

---

## 十五、设计总结

| 维度 | 方案 |
|------|------|
| 模板管理 | 每个保司 × 险种 × 版本独立 JSON，多版本共存 |
| 字段提取 | 三层全量提取（公共 + 险种专属 + 保司特有），used 标记按需启用 |
| 规则体系 | BaseRule → 险种基础规则 → 保司版本规则，继承覆写 |
| 模板学习 | 上传 3-5 份样本自动生成模板+规则，自验证通过后注册，无需人工写正则 |
| OCR | 有文本层直接用（最准）；扫描件多引擎并行 + 交叉比对 |
| 变动检测 | 指纹匹配度 + 必填字段缺失 + 布局偏移 + OCR 差异 |
| 自动发现 | 低匹配保单自动收集 → 自动训练新版本 → 人工确认注册 |
| 告警 | 数据库记录 + 钉钉推送，区分 info/warning/critical |
| 迁移 | 渐进式 7 阶段，新旧并行验证，不停机 |
