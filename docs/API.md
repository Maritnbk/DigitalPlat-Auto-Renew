# DigitalPlat One API 速查（本项目用到的部分）

来源：DigitalPlat 官方 API 文档（`DigitalPlat One` → API Documentation）。本文是其中与自动续期相关部分的摘录与实测补充；原始网页快照 `DigitalPlat One API.mhtml` 体积较大，未纳入仓库。

- **Base URL**：`https://domain-api.digitalplat.org/api/v1`
- **认证**：`Authorization: Bearer dp_live_xxxxx`
- **统一响应信封**：

  ```json
  { "success": true, "data": { }, "meta": { } }
  ```

  Key 在服务端以 SHA256 存储，**原始 Key 只在创建时显示一次**。
- **Key 前缀**：`dp_live_`（生产）/ `dp_test_`（沙箱）。

## 1. 列出域名

```
GET /api/v1/domains
```

响应示例：

```json
{
  "success": true,
  "data": [
    {
      "domain": "example.us.kg",
      "status": "ok",
      "slot_type": "subscription",
      "lifecycle_type": "subscription",
      "expires_at": "2027-04-08",
      "nameservers": ["ns1.provider.com", "ns2.provider.com"]
    }
  ],
  "meta": {}
}
```

字段说明（本项目使用）：

| 字段 | 用途 |
| --- | --- |
| `domain` | 域名全名，也是续期接口的路径参数 |
| `status` | `ok` / `active` 视为正常；`pendingdelete` 视为待释放 |
| `slot_type` | `free` / `paid` / `subscription`，仅用于报告统计 |
| `lifecycle_type` | `permanent` 表示永久有效，无需续期 |
| `expires_at` | 到期日，文档示例为纯日期 `YYYY-MM-DD`，脚本同时兼容 ISO 时间戳与 epoch |

## 2. 续期域名

```
POST /api/v1/domains/{domain}/renew
```

请求体：

```json
{ "years": 1, "payment_method": "sandbox" }
```

- `years`：续期年数。
- `payment_method`：**可选**。文档示例里出现的是 `sandbox`（沙箱环境），注册订单接口用的是 `credits`。
  本项目默认**不发送**该字段，交由服务端走「当前环境认可的支付流程」；需要显式指定时用仓库变量
  `DIGITALPLAT_PAYMENT_METHOD` 覆盖。

响应示例（文档原文）：

```json
{ "success": true, "data": { "domain_kind": "registrar", "registry_status": "active" }, "meta": {} }
```

> ⚠️ 该响应**不包含新的到期时间**。因此脚本续期成功后会再调一次 `GET /domains` 复核到期日；
> 若到期日没变，报告里会显式写「接口返回成功，但到期时间未变化，请人工核查」，而不是直接报成功。

## 3. 错误处理策略

实测 401 返回的错误结构与文档示例不同（嵌套 `error` 对象）：

```json
{ "error": { "code": "invalid_api_key", "message": "Invalid API key" }, "success": false }
```

脚本同时兼容嵌套与扁平两种结构，并优先读取 body 里的 `code`，而不是只看 HTTP 状态码——
否则日志里只会剩下「401 Client Error」，把 `invalid_api_key` 这种可行动信息丢掉。

已知的错误码与处理建议见 `renew_domains.py` 中的 `ERROR_HINTS`。

### 「未进入续期窗口」属于正常结果

DigitalPlat 规定距到期 120 天以外不允许续期。接口对这种情况可能返回：

- 明确的错误码（如 `renewal_not_yet_available`）
- 或一段文本（如 `Domain expires more than 120 days from now`）

脚本用 `BENIGN_CODES` + `BENIGN_MESSAGE_PATTERNS` 双重识别，命中时记为 `⏭️ 无需续期`，**不计入失败**，
因此不会让工作流标红，也不会触发告警。

### 重试策略

| 请求类型 | 重试 | 原因 |
| --- | --- | --- |
| `GET /domains`（只读） | 连接/读超时/5xx/429 全重试，最多 4 次，指数退避 | 只读，重试无副作用 |
| `POST /renew`（写） | 只对 **connect 错误** 和 **429** 重试 | 读超时和 5xx 时服务端可能已续期成功，重试会重复下单 |
| 通知推送 | 3 次重试 | 收不到通知比重复推送更糟 |

续期请求还会带上 `Idempotency-Key: <uuid4>`，配合服务端的幂等语义降低重复续期风险。

## 4. 相关但本项目未使用的端点

| 端点 | 说明 |
| --- | --- |
| `PATCH /api/v1/domains/{domain}/nameservers` | 修改 NS |
| `DELETE /api/v1/domains/{domain}` | 删除域名（进入 pendingdelete，7 天后释放） |
| `GET /api/v1/domains/{domain}/dns` | Hosted DNS 状态 |
| `POST /api/v1/domains/{domain}/dns/records` | 创建 DNS 记录（需 `Idempotency-Key`） |
| `POST /api/v1/registry/quotes` | 注册/续期/转移/恢复报价（10 分钟有效） |
| `POST /api/v1/registry/orders` | 用报价创建注册订单（需 `Idempotency-Key`） |
| `PATCH /api/v1/domains/{domain}/contacts` | 更新注册联系人 |
| `PUT /api/v1/domains/{domain}/dnssec` | 设置 DNSSEC DS 记录 |

> 注册（而非续期）走的是 `registry/quotes` → `registry/orders` 两步流程，需要 `contact_profile_id`、
> `accept_agreement` 与 `agreement_version`，与自动续期的场景无关，故本项目不涉及。
