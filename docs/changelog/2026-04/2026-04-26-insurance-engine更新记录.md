# 2026-04-26 保单结构化识别引擎

## 需求说明

重构保单识别系统，实现：
1. 每个保司 × 险种 × 版本独立模板管理，多版本共存
2. 全量字段三层提取（公共/险种专属/保司特有），used 标记按需启用
3. 模板自动学习（上传 3-5 份样本 PDF 自动生成模板和规则，无需人工写正则）
4. 扫描件多 OCR 交叉验证（百度/腾讯并行，关键字段差异标记）
5. 有文本层的 PDF 直接用 pdfplumber（最准），跳过云 OCR
6. 结构变动自动检测 + 钉钉告警
7. 低匹配保单自动收集，积累样本后触发训练

## 数据库变更

### 新增表
- `template_alert_records` — 模板告警记录（template_id, match_score, alert_level, alert_type, resolved 等）
- `template_train_records` — 训练记录（company_id, policy_type, version, hit_rate, rule_code 等）

### insurance_records 新增字段
- `template_id` VARCHAR(128) — 匹配的模板版本
- `match_score` FLOAT — 模板匹配度
- `field_layer_data` LONGTEXT — 三层结构化字段 JSON
- `ocr_engines` VARCHAR(128) — 使用的 OCR 引擎
- `ocr_cross_validated` TINYINT — 是否交叉验证
- `ocr_diffs` LONGTEXT — OCR 差异详情
- `has_critical_diff` TINYINT — 是否有关键差异

## 代码变更

### 新增模块
- `insurance/engine/` — 识别引擎
  - `schema.py` — 数据类型定义（9 个 dataclass）
  - `company_identifier.py` — 保司识别（4 层策略）
  - `type_identifier.py` — 险种识别（30+ 正则模式）
  - `template_matcher.py` — 模板版本匹配（指纹匹配度计算）
  - `field_extractor.py` — 字段提取调度器（动态加载规则）
  - `structure_validator.py` — 结构校验 + 变动检测
  - `pipeline.py` — 主流水线（串联 6 个 Stage）
- `insurance/ocr/` — OCR 引擎层
  - `base.py` — 引擎基类 + OCRResult
  - `pdfplumber_engine.py` — pdfplumber 文本层提取
  - `baidu_engine.py` — 百度云 OCR
  - `tencent_engine.py` — 腾讯云 OCR（TC3-HMAC-SHA256 签名）
  - `dispatcher.py` — 多引擎调度（文本层直用/扫描件并行）
  - `comparator.py` — 交叉比对 + 投票合并
- `insurance/rules/` — 规则体系
  - `base_rule.py` — 基础规则（所有公共字段提取）
  - `base_compulsory.py` — 交强险基础规则
  - `base_commercial.py` — 商业险基础规则
  - `base_accident.py` — 驾意险基础规则
- `insurance/templates/` — 模板库
  - `registry.json` — 模板注册表
- `insurance/learner/` — 自动学习引擎
  - `text_aligner.py` — 多文本结构对齐
  - `field_discoverer.py` — 字段自动发现
  - `pattern_inducer.py` — 正则自动归纳
  - `rule_generator.py` — 规则代码生成
  - `template_generator.py` — 模板 JSON 生成
  - `self_validator.py` — 自验证
  - `trainer.py` — 训练器主入口
- `insurance/alerts/` — 告警模块
  - `notifier.py` — 告警通知（数据库 + 钉钉）
  - `collector.py` — 低匹配样本收集

### 修改文件
- `insurance/db.py` — 新增表 + 新增字段 + 4 个新 DB 操作函数
- `insurance/handler.py` — 集成新引擎（新引擎优先，旧 policy_parser 降级）
- `insurance/api.py` — 新增训练/告警 API

### 新增 API
| 方法 | 路由 | 说明 |
|------|------|------|
| POST | `/api/insurance/train` | 上传样本 PDF 训练模板 |
| GET | `/api/insurance/alerts` | 查询告警记录 |
| POST | `/api/insurance/alerts/<id>/resolve` | 标记告警已处理 |

## 部署步骤

1. 拉取最新代码
2. 重启服务（数据库表和字段自动创建）
3. 通过 `POST /api/insurance/train` 上传各保司样本，生成模板和规则
4. 新引擎自动启用，旧 `policy_parser.py` 作为降级后备

## 设计文档

详见 `docs/design/2026-04-26-insurance-engine-design.md`
