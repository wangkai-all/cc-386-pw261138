# NormLink · 跨系统字符串归一与关联

把来自 ERP / CRM / MDM / 台账 / 供应商门户 / 老 AS/400 等系统的同一实体字符串
（`ＡＣＭＥ　ＧｍｂＨ`、`ACME​ GmbH`、`  acme   gmbh ` …）归一成稳定的规范键并关联，
同时保证**旧报告不被新规则悄悄改写**、**冲突行不被自动吞掉**、**发布结果不混用规则世代**。

纯 Python 标准库实现（无第三方运行时依赖），三个服务各自独立进程、各自 SQLite，
只通过 HTTP 协作。

---

## 1. 一分钟跑起来

```bash
# 从空目录启动全部服务（raw 8801 / normalizer 8802 / linkage 8803 / UI 8800）
python3 scripts/dev.py --data-dir data --clean

# 另开终端：一条命令完成 录入 → 规则 → 全量构建 → 发布 → 下载证据
python3 scripts/demo.py --data-dir /tmp/normlink-demo
```

打开 <http://127.0.0.1:8800/> 即为操作台。`scripts/demo.py` 会在 `artifacts/`
下写出可下载的映射依据（见第 6 节）。

其他入口：

```bash
python3 -m unittest discover -s tests     # 32 个单测 + 真实 HTTP 集成测试
python3 scripts/e2e.py --base http://127.0.0.1:8800   # 60 项端到端检查
node scripts/ui_smoke.mjs --base http://127.0.0.1:8800  # jsdom 界面冒烟（22 项）
```

> `ui_smoke.mjs` 依赖 `/tmp/uitest/node_modules/jsdom`；沙箱无完整浏览器，界面验证用 jsdom。

---

## 2. 三个服务，各自运行

| 服务 | 端口 | 数据 | 职责 |
| --- | --- | --- | --- |
| 原值接收 `raw` | 8801 | `raw.db` | 收到的字符串**逐字符原样**保存；同一来源+外部 ID 的变化产生新版本（revision），旧版本标记为 inactive；批次与指纹可追溯 |
| 规范化映射 `normalizer` | 8802 | `normalizer.db` | 规则世代（draft/published/superseded）、不可变的规范化运行、每一步的证据 |
| 关联结果 `linkage` | 8803 | `linkage.db` | 把一次固定世代、固定运行的映射结果物化成等价类与发布；碰撞、裁决、影响对比 |
| 操作台 `ui` | 8800 | 无 | 静态页面 + `/api/{raw,norm,link}` 反向代理 |

linkage 不直接读另外两个服务的数据库，只调用它们的 HTTP API：
`GET /snapshot`、`GET /records`、`POST /runs`、`GET /runs/{id}/items`、`GET /rules/current`。

启动单个服务：

```bash
python3 -m app.server raw        --port 8801 --data-dir data/raw
python3 -m app.server normalizer --port 8802 --data-dir data/normalizer
python3 -m app.server linkage    --port 8803 --data-dir data/linkage \
        --raw-base http://127.0.0.1:8801 --normalizer-base http://127.0.0.1:8802
```

---

## 3. 核心不变量

1. **原值永远可还原。** 入库不做任何清洗；`/records/{id}`、`report.json`、CSV 都同时给出
   原文、`\uXXXX` 转义形式与逐字符码点（名称、Unicode 类别）。全角、表意空格 U+3000、
   NBSP、零宽空格 U+200B 都原样保留。
2. **规则出版后不可变。** 发布（published）的世代不能再改；编辑已发布世代会在 UI 中
   自动派生一个新的 draft。`steps_hash` 只由生效步骤决定，改备注不会制造新世代。
3. **一次运行绑定一个世代。** `/runs` 记录 `generation_id + steps_hash` 和输入指纹
   （`raw_id:sha256` 排序哈希）。输入快照对不上就拒绝运行，避免“用一半旧数据算出的新键”。
4. **发布即固化。** 关联发布（release）保存 `generation_id / generation_hash / run_id`，
   所有 class/member 行都带 `release_id`。历史报告按 release 读取，与“当前规则”无关；
   报告内容哈希不含 `generated_at`，规则再变也字节稳定。
5. **发布时同世代。** 构建开始前记录世代；写库前再向 normalizer 复核当前世代，
   不一致则放弃发布（`409`）。激活非当前世代的预览同样被拒绝，且必须提交
   `expected_generation_hash`。
6. **冲突不自动吞。** 一个规范键下出现 ≥2 种不同原值（按 `raw_sha256` 去重）即成为
   `conflict` 类：全部成员保留、全部来源列出、逐条给出证据。空键（`""`）单独成类，
   不与正常实体混排。
7. **裁决有边界。** 裁决记录的是**当时那批原值**的 sha 集合；只有集合完全一致才应用。
   键下新增了不同原值 → 该裁决变成“需复核”（`conflict_needs_review` / adjudication gap），
   不会顺手套到新成员上。撤销裁决后，下一次增量会强制重算该键。
8. **增量不越权。** 规则世代变了、或有活动原值被撤回，增量构建直接拒绝并提示全量重建；
   人工裁决新增/撤销/失效的键会被强制拉入增量的重算范围。
9. **构建可中断。** 构建写入 staging 发布；取消标志在每个阶段轮询，取消后清掉 staging
   行，现有 active 发布保持不变（指针切换在单个 `BEGIN IMMEDIATE` 事务里完成）。

---

## 4. 规则与证据

支持的操作（`app/normalization.py`，UI 可增删、上下移动、逐项配参）：

`unicode_nfkc/nfc/nfd/nfkd`、`strip_diacritics`、`trim`、`collapse_ws`、`remove_ws`、
`casefold`、`lower`、`upper`、`strip_format`、`strip_control`、`strip_punct`（`keep` / `also_symbols`）、
`replace_map`（整值别名）、`regex_replace`（`i/m/s/x`，带灾难性回溯启发式校验）。

每次规范化对每条原值、每一步都记录：

- 步骤输入/输出（展示用截断 400 字符）与完整 `raw` / `key`（永不截断）
- `\uXXXX` 转义串、逐字符码点（`U+FF21` + 名称 + 类别）
- 该步的变化片段（difflib 对齐，前后转义与码点）
- 哪些步骤真正改变了值（`changed_steps`）

`POST /normalize/preview` 用同一引擎在内存里试算，不落库、不产生运行；
UI 的规则页用它做即时预览。

---

## 5. 两种构建模式的代价

| | 全量 `full` | 增量 `incremental` |
| --- | --- | --- |
| 何时用 | 规则变化、首次构建、或需要完全重算 | 规则世代不变，只有少量原值新增/修改 |
| 读什么 | 全部活动原值 | 变化的原值 + 受影响键的既有成员（含裁决相关键） |
| 重算什么 | 每条原值全步骤规范化 + 全部键重新分组 | 只对上述子集跑规范化，其余成员沿用父发布的键 |
| 代价 | O(全部行 × 步数) | O(受影响行 × 步数)，但必须持有父发布 |
| 硬性限制 | 无 | 世代变化 / 原值撤回 → 拒绝；无活动发布 → 拒绝 |

两个模式都会在 `job.impact` 中给出影响范围：受影响键数、合并/拆分/新增/移除的类数、
移动成员数、碰撞类数与需复核的裁决数。UI 的“影响对比”可对比任意两个发布的
`/diff`（merges / splits / added / removed / moved，附示例原值）。

---

## 6. 碰撞证据与下载

`GET /releases/{id}/conflicts` 返回每个碰撞类的成员（原文、转义、码点）与
`difference_profile`：按累积阶段（`invisible` → `case_or_width` → `whitespace` →
`diacritic` → `punctuation` → `characters`）解释差异来源，并给出：

- `severity = variant`：差异可由某个写法因素完整解释（零宽、全半角、大小写、空白、变音、标点）
- `severity = substantive`：以上都抹平后仍不同，可能把不同实体并在一起，**优先人工裁决**
- `severity = empty_key`：空键隔离类
- `worst_member`：实质差异时具体是哪条原值造成的

这是分诊提示，不改变分组结果——分组始终由规则决定，冲突始终保留。

可下载（UI 与 demo 脚本都直接走这些地址）：

| 文件 | 地址 | 内容 |
| --- | --- | --- |
| `report.json` | `/api/link/releases/{id}/report.json` | 发布、类、成员、已应用裁决、内容哈希、裁决缺口 |
| `classes.csv` | `/api/link/releases/{id}/classes.csv` | 等价类与状态 |
| `members.csv` | `/api/link/releases/{id}/members.csv` | 每个成员的原值/转义/hash/键 |
| `evidence.csv` | `/api/norm/runs/{run}/evidence.csv` | **每条原值逐步**的输入输出、转义、码点、变化 |
| `mapping.csv` | `/api/norm/runs/{run}/mapping.csv` | 原值 → 规范键映射 |
| `rules.json` | `/api/norm/runs/{run}/rules.json` | 该运行固定的规则世代与输入指纹 |

CSV 均为 UTF-8 BOM + CRLF，Excel 直接打开不乱码。

---

## 7. 人工裁决

在“碰撞与裁决”页对任一隔离键选择：

- **接受合并**：确认这些原值属于同一实体 → 构建后成为 `adjudicated_merge` 类，仍保留全部成员与证据
- **拆分**：把不同原值分组，每组一个独立等价类 → `adjudicated_split`
- **保持隔离**：保留 `hold` 状态，不做合并，后续可再裁

裁决只在下一次构建时生效，**不会改写任何已发布报告**。同一键同时只允许一条生效裁决；
撤销后重新构建才会恢复。发布的 `report.json` 中有冻结的 `adjudications_applied` 快照，
其中 `revoked_later` 标记“曾应用、后来被撤销”，历史可审计。

---

## 8. HTTP API 速查

**raw**：`GET /health`、`POST /ingest`（JSON 或 `text/csv`）、`GET /active_ids`、
`GET /records`（`ids=` / `since_id=` / `q=` / `source_system=`）、`GET /records/{id}`、
`GET /snapshot`、`GET /sources`、`GET /batches`、`GET /export.csv`

**normalizer**：`GET /health`、`GET /rules`、`GET /rules/current`、
`GET|POST /rules/generations`、`PUT /rules/generations/{id}`、`POST /rules/generations/{id}/publish`、
`POST /normalize/preview`、`POST /runs`、`GET /runs`、`GET /runs/{id}`、`GET /runs/{id}/keys`、
`GET /runs/{id}/items`、`GET /runs/{id}/items/{raw_id}`、`GET /runs/{id}/collisions`、
`GET /runs/{id}/evidence.csv|mapping.csv|rules.json`、`GET /diff?from_run=&to_run=`

**linkage**：`GET /health`、`GET /overview`、`POST /builds`、`GET /jobs`、`GET /jobs/{id}`、
`POST /jobs/{id}/cancel`、`GET /releases`、`GET /releases/active`、`GET /releases/{id}`、
`POST /releases/{id}/activate`、`GET /releases/{id}/classes`、`/classes/{class_id}`、
`/conflicts`、`/members/{raw_id}`、`/adjudication-gaps`、`/report.json`、`/classes.csv`、`/members.csv`、
`GET /diff?base=&candidate=`、`GET|POST /adjudications`、`POST /adjudications/{id}/revoke`

---

## 9. 目录

```
app/
  common.py              SQLite(WAL, 事务)、路由、HTTP 客户端、CSV/JSON 响应
  normalization.py       规则校验与归一引擎、逐字符证据
  raw_service.py         原值接收服务
  normalizer_service.py  规则世代 + 规范化运行
  link_service.py        等价类物化、发布、碰撞分诊、裁决、差异
  server.py              单服务进程入口
  ui/                    操作台（原生 HTML/CSS/JS，无构建步骤）
scripts/
  dev.py                 一键启动四个进程
  demo.py                空目录 → 完整演示 + 下载证据
  make_seed.py           生成跨系统种子数据（246 行，含各类难字符）
  e2e.py                 60 项端到端检查
  ui_smoke.mjs           jsdom 界面冒烟测试
seed/                    种子 CSV 与期望值/码点审计
tests/                   单测与真实 HTTP 集成测试
data/  artifacts/        运行期数据与下载样例（已 gitignore）
```

---

## 10. 边界与取舍

- 原值接收**没有撤回接口**：活动集合只由 revision 更替产生。增量若发现活动记录消失
  （例如手工改库）会拒绝并建议全量，避免静默丢失。
- 增量不是“局部替换类”：只要某个键受影响，该键下存活成员会一起进入新发布，
  保证同类成员始终来自同一份输入；没有变化的键沿父发布的键值。
- 规范化在 Python 进程内完成，适合十万级；更大规模应把 `runs` 落成批处理并分片，
  但世代/运行/发布三者的固化关系不需要变。
- 界面无第三方前端依赖；复杂交互（拖拽排序等）刻意用按钮实现，避免构建链。
