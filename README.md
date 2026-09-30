# 实验室测量可信性追溯服务

基于 **Python 3.11 + FastAPI + SQLite** 的校准证书 / 测量读数可信性追溯 HTTP 服务。

- 校准证书：证书编号、设备编号、UTC **左闭右开**有效期 `[valid_from, valid_to)`、**闭区间**允许值范围 `[min_value, max_value]`。
- 测量读数：读数编号、设备编号、UTC 测量时间、数值。
- 每次成功变更返回**全局单调递增版本号**；所有写请求必须带 `request_id`（请求标识）。
- 证书只能**撤销**或由新证书版本**纠正**：纠正后旧版本在新版本起失效，历史只追加、永不覆盖。
- 读数按测量时间匹配**指定提交版本视角**下的有效证书：
  - 无匹配 → `unverified`（未验证）
  - 多份匹配 → `review`（待复核）
  - 唯一匹配且数值在闭区间内 → `conforming`（合格）
  - 唯一匹配但数值超出闭区间 → `out_of_range`（超限）
- 撤销 / 纠正会改变当前受影响读数的结论，并在响应中给出前后对照；旧版本的结论与所依证书仍可随时查询。
- **幂等**：相同 `request_id` + 相同内容重复提交不产生新版本（返回首次结果，`idempotent_replay=true`）；`request_id` 复用但内容不同返回 `409`；无效区间 / 未知目标返回 `422` / `404`，单事务保证不留下任何部分变更。

---

## 一、启动

### 方式 A：Docker Compose（推荐，一键启动并初始化数据库）

```bash
docker compose up -d --build
```

服务启动时会自动在数据卷中创建并初始化 SQLite 数据库（表不存在才建，幂等可重复执行）。

查看状态 / 日志 / 停止：

```bash
docker compose ps
docker compose logs -f
docker compose down            # 停止并删除容器，数据卷保留
docker compose down -v         # 同时删除数据库数据卷
```

数据库文件位于容器内 `/data/trace.db`，通过命名卷 `trace-data` 持久化。

### 方式 B：本地 Python 运行

```bash
pip install -r requirements.txt
export TRACE_DB_PATH=./data/trace.db   # 可选，默认即此值
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

> 需要 Python 3.11+。首次启动自动建库（会自动创建 `./data/` 目录）。

---

## 二、地址与端口

| 用途 | 地址 |
| --- | --- |
| 服务根地址 | <http://localhost:8080/> |
| 健康检查 | `GET http://localhost:8080/api/v1/health` |
| API 前缀 | `http://localhost:8080/api/v1` |
| Swagger 交互文档 | <http://localhost:8080/docs> |
| OpenAPI 描述 | <http://localhost:8080/openapi.json> |

---

## 三、配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `TRACE_DB_PATH` | `./data/trace.db`（容器内 `/data/trace.db`） | SQLite 数据库文件路径 |
| `TRACE_HOST` | `0.0.0.0` | 监听地址 |
| `TRACE_PORT` | `8080` | 监听端口 |
| `TRACE_BUSY_TIMEOUT_MS` | `5000` | SQLite 写锁等待毫秒数 |
| `TRACE_MAX_PAGE_SIZE` | `200` | 列表接口单页最大条数 |

Compose 中可直接修改 `docker-compose.yml` 的 `environment` 与 `ports` 映射。

---

## 四、数据模型约定

- 所有时间必须**带时区**，服务端统一归一化为 UTC，例：`2026-01-01T00:00:00Z` 或 `2026-01-01T08:00:00+08:00`。
- 有效期：`valid_from <= measured_at < valid_to`（左闭右开，右边界时刻不匹配）。
- 允许值：`min_value <= value <= max_value`（闭区间，边界值合格）。
- 写请求体必须含非空 `request_id`（≤128 字符）。
- 错误响应统一格式：

  ```json
  { "error": { "code": "request_id_conflict", "message": "..." } }
  ```

---

## 五、调用示例

以下示例可直接用 `curl` 执行。

### 1. 签发校准证书

```bash
curl -s -X POST http://localhost:8080/api/v1/certificates \
  -H 'Content-Type: application/json' \
  -d '{
    "request_id": "req-001",
    "certificate_no": "CERT-001",
    "device_no": "DEV-A",
    "valid_from": "2026-01-01T00:00:00Z",
    "valid_to":   "2026-07-01T00:00:00Z",
    "min_value": 0.0,
    "max_value": 100.0
  }'
```

返回：

```json
{
  "version": 1,
  "request_id": "req-001",
  "event_type": "cert_issue",
  "idempotent_replay": false,
  "certificate": {
    "certificate_no": "CERT-001", "version_seq": 1,
    "device_no": "DEV-A",
    "valid_from": "2026-01-01T00:00:00.000000Z",
    "valid_to":   "2026-07-01T00:00:00.000000Z",
    "min_value": 0.0, "max_value": 100.0,
    "created_at_version": 1, "superseded_at_version": null
  }
}
```

### 2. 提交读数（支持批量，立即返回当前版本视角下的结论）

```bash
curl -s -X POST http://localhost:8080/api/v1/readings \
  -H 'Content-Type: application/json' \
  -d '{
    "request_id": "req-002",
    "readings": [
      {"reading_no": "M-1", "device_no": "DEV-A", "measured_at": "2026-03-01T10:00:00Z", "value": 42.0},
      {"reading_no": "M-2", "device_no": "DEV-A", "measured_at": "2026-03-01T11:00:00Z", "value": 140.0},
      {"reading_no": "M-3", "device_no": "DEV-B", "measured_at": "2026-03-01T12:00:00Z", "value": 1.0}
    ]
  }'
```

- `M-1` 唯一匹配且在范围内 → `conforming`
- `M-2` 唯一匹配但超范围 → `out_of_range`
- `M-3` 设备无证书覆盖 → `unverified`

### 3. 待复核：同一测量时刻有多份有效证书

再签发一份覆盖同一时刻的证书后，`M-1` 的当前结论变为 `review`，响应中 `candidates` 列出全部候选证书。

### 4. 纠正证书（新版本使旧版本失效）

```bash
curl -s -X POST http://localhost:8080/api/v1/certificates/CERT-001/correct \
  -H 'Content-Type: application/json' \
  -d '{
    "request_id": "req-003",
    "certificate_no": "CERT-001",
    "device_no": "DEV-A",
    "valid_from": "2026-01-01T00:00:00Z",
    "valid_to":   "2026-07-01T00:00:00Z",
    "min_value": 50.0,
    "max_value": 100.0,
    "reason": "允许范围纠正为 50-100"
  }'
```

返回新版本号，`superseded_version_seq` 指向被失效的旧版本，`affected_readings` 给出相关设备上所有读数的前后结论对照（例如 `M-1：conforming -> out_of_range`）。旧版本内容仍保留在历史中。

### 5. 撤销证书

```bash
curl -s -X POST http://localhost:8080/api/v1/certificates/CERT-001/revoke \
  -H 'Content-Type: application/json' \
  -d '{"request_id": "req-004", "certificate_no": "CERT-001", "reason": "证书作废"}'
```

受影响读数结论随之改变（例如 `out_of_range -> unverified`）。撤销后不能再纠正，重复撤销返回 `409`。

### 6. 幂等重放与冲突

```bash
# 与步骤 1 完全相同的 request_id + 内容：不产生新版本，返回首次结果
curl -s -X POST http://localhost:8080/api/v1/certificates \
  -H 'Content-Type: application/json' -d '{ ...同步骤1... }'
# -> 200，"version": 1，"idempotent_replay": true

# 同一 request_id 但内容不同：
# -> 409 {"error":{"code":"request_id_conflict", ...}}，无任何数据落库
```

### 7. 查询

```bash
# 读数当前结论
curl -s http://localhost:8080/api/v1/readings/M-1

# 读数在历史版本视角下的结论（旧结论及所依证书仍可查）
curl -s "http://localhost:8080/api/v1/readings/M-1?at_version=2"

# 按设备 / 结论过滤读数
curl -s "http://localhost:8080/api/v1/readings?device_no=DEV-A&status=out_of_range"

# 证书当前版本
curl -s http://localhost:8080/api/v1/certificates/CERT-001

# 证书在指定版本视角下的内容
curl -s "http://localhost:8080/api/v1/certificates/CERT-001?at_version=1"

# 证书完整版本流水（不可变历史）
curl -s http://localhost:8080/api/v1/certificates/CERT-001/history

# 全局版本流水（每次成功变更一条）
curl -s "http://localhost:8080/api/v1/versions?limit=50"

# 按请求标识追溯原始请求与响应
curl -s http://localhost:8080/api/v1/requests/req-003

# 健康检查 / 当前版本
curl -s http://localhost:8080/api/v1/health
curl -s http://localhost:8080/api/v1/status
```

### 8. 审计取证：生成追溯包（本轮新增）

按 **设备编号 + 带时区测量时间的左闭右开区间 `[time_from, time_to)` + 已提交版本 `at_version`** 生成追溯包。包只收录该版本前（含）已提交（`submitted_at_version <= at_version`）、设备相同且测量时刻落在区间内的读数；每条读数给出在 `at_version` 视角下的结论与全部候选证书快照。

```bash
# 生成（或幂等复用）追溯包；page_size 为分段大小，缺省取服务默认值
curl -s -X POST http://localhost:8080/api/v1/audit/packages \
  -H 'Content-Type: application/json' \
  -d '{
    "device_no": "DEV-A",
    "time_from": "2026-01-01T00:00:00Z",
    "time_to":   "2026-07-01T00:00:00Z",
    "at_version": 2,
    "page_size": 100
  }'
```

返回清单：筛选条件 `criteria`、`at_version`、`total_count`、固定分段表 `segments`（含每段偏移与段摘要）、`entries_digest`（全量条目摘要）、`package_digest`（清单摘要）、`canonicalization`（规范化规则）与 `package_id`。区间内无匹配读数时返回 `total_count=0`、无分段的**空包**（不报错）。

```bash
# 分段获取冻结条目（段号从 0 起；按段序拼接即可无遗漏、无重复地复原整包）
curl -s http://localhost:8080/api/v1/audit/packages/AP_ID/segments/0
curl -s http://localhost:8080/api/v1/audit/packages/AP_ID/segments/1

# 查包清单（摘要、分段表）
curl -s http://localhost:8080/api/v1/audit/packages/AP_ID

# 按筛选条件+版本定位既有包（不存在返回 404，不会隐式生成）
curl -s -G http://localhost:8080/api/v1/audit/packages/find \
  --data-urlencode device_no=DEV-A \
  --data-urlencode 'time_from=2026-01-01T00:00:00Z' \
  --data-urlencode 'time_to=2026-07-01T00:00:00Z' \
  --data-urlencode at_version=2
```

时间可带任意时区（如 `2026-01-01T08:00:00+08:00`），服务端归一化为 UTC；等价条件复用同一个包。**完整性摘要的规范化规则**随包在 `canonicalization` 字段给出（规则版本 `1`）：

- JSON：UTF-8、紧凑分隔符、非 ASCII 不转义、对象键按 Unicode 码点排序（`json.dumps(sort_keys=True, separators=(",",":"), ensure_ascii=False)`）；时间一律 UTC 定长 `YYYY-MM-DDTHH:MM:SS.ffffffZ`；
- 条目顺序：`measured_at` 升序，再按 `reading_no` 升序；候选证书按 `certificate_no`、`version_seq` 排序；
- `segment_digest = SHA-256(段内各条目规范化 JSON 字节按序拼接)`，小写十六进制；
- `entries_digest = SHA-256(全部条目规范化 JSON 字节按序拼接)`，空集为 `SHA-256("") = e3b0c442…`；
- `package_digest = SHA-256(规范化清单 JSON 的 UTF-8 字节)`。审计方仅凭 HTTP 取回的 JSON 即可独立复算全部摘要。

追溯包是**创建时冻结**的只追加快照：生成在单条 `BEGIN IMMEDIATE` 事务内完成，期间的新提交、证书纠正或撤销被写锁阻塞，不会混入；生成之后的任何变更也不影响既有包。相同筛选条件与版本唯一对应一个包，内容与摘要必然一致（重复请求返回同一 `package_id`，`reused=true`）。追溯包是审计派生物，不占用 `versions` 表的全局领域版本号。

---

## 六、HTTP 接口一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/v1/certificates` | 签发证书 |
| POST | `/api/v1/certificates/{cert_no}/correct` | 纠正证书（追加新版本，旧版本失效） |
| POST | `/api/v1/certificates/{cert_no}/revoke` | 撤销证书 |
| GET  | `/api/v1/certificates` | 证书列表（`at_version` / `device_no` / 分页） |
| GET  | `/api/v1/certificates/{cert_no}` | 证书当前/指定版本视角内容 |
| GET  | `/api/v1/certificates/{cert_no}/history` | 证书版本历史 |
| POST | `/api/v1/readings` | 批量提交读数（读数不可变） |
| GET  | `/api/v1/readings` | 读数列表（`at_version` / `device_no` / `status` / 分页） |
| GET  | `/api/v1/readings/{reading_no}` | 读数结论（默认当前版本，`at_version` 查历史） |
| GET  | `/api/v1/versions` | 全局单调版本流水 |
| GET  | `/api/v1/requests/{request_id}` | 按请求标识追溯 |
| POST | `/api/v1/audit/packages` | 生成（幂等复用）审计取证追溯包 |
| GET  | `/api/v1/audit/packages/{package_id}` | 追溯包清单与摘要 |
| GET  | `/api/v1/audit/packages/{package_id}/segments/{index}` | 分段获取冻结条目（段号从 0 起） |
| GET  | `/api/v1/audit/packages/find` | 按条件+版本定位既有包（只读，404=无） |
| GET  | `/api/v1/health` | 健康检查 |

主要状态码：`200` 成功；`201` 追溯包创建 / 幂等复用；`404` 未知目标（证书 / 读数 / 请求 / 追溯包 / 分段）；`409` 请求标识冲突、证书已存在 / 已撤销、读数已存在；`422` 参数校验失败（含无效时间区间、范围倒置、缺时区、版本越界、页大小越界等）。

---

## 七、设计要点

- **单调整局版本**：`versions` 表自增主键，四类事件 `cert_issue / cert_correct / cert_revoke / reading_submit` 全部追加，记录请求体、指纹与响应快照。
- **历史不可覆盖**：证书内容存为只追加的 `certificate_versions`，纠正 / 撤销仅在旧行登记 `superseded_at_version`；读数只追加。
- **版本视角查询**：证书在版本 V 有效 ⇔ 其内容版本 `created_at_version <= V` 且（`superseded_at_version` 为空或 `> V`），且测量时刻落在 `[valid_from, valid_to)`。
- **事务与并发**：所有写操作在单条 `BEGIN IMMEDIATE` 事务内完成「幂等判定 → 业务校验 → 落库」，任何错误整体回滚；WAL 模式支持读写并发。
- **幂等指纹**：对归一化（UTC 化）后的请求体做排序 JSON 指纹；相同 `request_id` + 指纹返回首次响应，`request_id` 复用且指纹不同则拒绝。
- **审计追溯包冻结模型**：`audit_packages`（主档 + 规范化清单原文 + 两层摘要）与 `audit_package_entries`（逐条目规范化快照）只追加；创建在单条 `BEGIN IMMEDIATE` 事务内选数、按固定版本评估结论、分段并落库，`(device_no, time_from, time_to, at_version)` 唯一约束保证同条件+版本唯一且摘要一致；大量条目经固定 `page_size` 分段，`segment_index` 连续、偏移确定，拼接无重无漏。

---

## 八、测试

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

测试通过 TestClient 覆盖：四种结论、左闭右开边界、纠正 / 撤销的结论联动、旧版本可追溯、幂等重放、标识冲突、无效区间、未知目标、批次部分变更防护与多时区归一化；审计追溯包（`test_audit.py`）覆盖版本截止选数与半开边界、固定版本结论与全部候选证书快照、空包、分段拼接无重无漏、摘要按声明的规范化规则独立复算、同条件+版本幂等一致、生成后的提交 / 纠正 / 撤销不混入旧包、版本越界 / 无效区间明确报错且不留半成品。
