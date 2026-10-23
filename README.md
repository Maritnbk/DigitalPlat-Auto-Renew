# DigitalPlat-Auto-Renew

DigitalPlat 免费域名自动续期助手

利用 **DigitalPlat One API** 在 GitHub Actions 上每月自动检查并续期 DigitalPlat 免费域名，结果写入 Actions 日志与 Job Summary，并可选通过 Telegram / PushPlus 推送。

> 接口行为依据 DigitalPlat 官方 API 文档（见 [docs/API.md](docs/API.md)）。

## 🌟 功能特性

- **全自动续期**：每月 1 日北京时间 08:23 自动运行，只对**已进入续期窗口**的域名提交续期。
- **官方 API 直连**：使用 `https://domain-api.digitalplat.org/api/v1`，不需要浏览器、不需要 Cookie、不需要打码平台。
- **多账号支持**：一个 `DIGITALPLAT_API_KEY` 里放多个 Key（逗号或换行分隔）即可批量处理。
- **安全**：密钥只存 GitHub Secrets；日志与推送内容自动脱敏（`dp_live_…`、Bot Token 均会被替换）。
- **不静默失败**：未到窗口、永久域名、待释放域名、接口错误分别给出不同结论；真正的失败会让工作流标红。
- **零配置可见**：即使不配任何通知通道，结果也会写进 Actions 日志和 Job Summary 表格。
- **保持定时任务存活**：每次运行提交一个 `.keepalive` 时间戳，规避公开仓库 60 天无活动被停用 schedule 的问题。
- **可先演练**：`--dry-run` 只检查不提交，确认无误再正式跑。

## 🧭 工作原理

1. `GET /api/v1/domains` 拉取账号下全部域名（`domain` / `status` / `slot_type` / `lifecycle_type` / `expires_at`）。
2. 逐个判定：
   | 判定 | 条件 | 动作 |
   | --- | --- | --- |
   | `permanent` | `lifecycle_type` 为 permanent，或到期时间显示永久 | 跳过 |
   | `pendingdelete` | `status` 为 pendingdelete | 记为失败（需人工介入） |
   | `skip` | 剩余天数 > 阈值（默认 120） | 跳过，等下次 |
   | `renew` | 剩余天数 ≤ 阈值，或到期时间未知 | 提交续期 |
   | `invalid` | 记录缺少 `domain` 字段 | 记为失败 |
3. 对需要续期的域名调用 `POST /api/v1/domains/{domain}/renew`（带 `Idempotency-Key`）。
4. 续期成功后重新拉一次列表核对新到期时间；若接口返回成功但到期时间没变，报告里会显式标注「请人工核查」。
5. 汇总成报告 → 日志 + Job Summary + 通知通道。

## 🚀 快速上手

### 第一步：获取 API Key

1. 登录 [DigitalPlat Dashboard](https://dash.domain.digitalplat.org/)。
2. 进入 **API Keys** 页面，创建一个 Key。
3. 复制以 `dp_live_` 开头的 Key。**它只显示一次**，请立刻保存。

> `dp_test_` 开头的是测试 Key，对应 sandbox 环境，不要用于生产。

### 第二步：配置 GitHub 仓库

1. Fork 本仓库（或把文件上传到你自己的仓库）。
2. 进入 **Settings → Secrets and variables → Actions**。
3. 在 **Secrets** 里点 **New repository secret**，添加：

   | 名称 | 必填 | 说明 |
   | --- | --- | --- |
   | `DIGITALPLAT_API_KEY` | ✅ | DigitalPlat API Key，多个用逗号或换行分隔 |
   | `TELEGRAM_BOT_TOKEN` | ❌ | Telegram Bot Token |
   | `TELEGRAM_CHAT_ID` | ❌ | Telegram 目标会话 ID |
   | `PUSHPLUS_TOKEN` | ❌ | PushPlus Token |
   | `PUSHPLUS_TOPIC` | ❌ | PushPlus 群组编码 |

4. （可选）在 **Variables** 里添加非敏感配置，不配则用默认值：

   | 名称 | 默认值 | 说明 |
   | --- | --- | --- |
   | `DIGITALPLAT_THRESHOLD_DAYS` | `120` | 剩余天数 ≤ 该值才续期 |
   | `DIGITALPLAT_RENEWAL_YEARS` | `1` | 每次续期年数 |
   | `DIGITALPLAT_PAYMENT_METHOD` | 空 | 请求体里的 `payment_method`，留空则不发送 |
   | `DIGITALPLAT_MIN_INTERVAL` | `1.5` | 相邻两次 API 调用的最小间隔（秒） |
   | `DIGITALPLAT_TZ_OFFSET` | `8` | 计算剩余天数用的时区偏移，非中国时区务必修改 |
   | `DIGITALPLAT_API_BASE_URL` | 官方地址 | 一般无需修改 |

### 第三步：启用自动化

> ⚠️ **Fork 之后定时任务默认是关闭的**，必须手动启用，否则它永远不会自己运行。

1. 点仓库顶部的 **Actions**。
2. 若出现提示，点 **I understand my workflows, go ahead and enable them**。
3. 左侧选 **DigitalPlat-Auto-Renew**，点 **Enable workflow**。
4. 点 **Run workflow** 手动跑一次，`dry_run` 勾上先验证配置，确认无误后再关掉 `dry_run` 正式跑。

## 📅 运行计划

- **频率**：每月 1 日 UTC 00:23（北京时间 08:23）。刻意避开整点——GitHub 官方文档说明 `schedule` 在高负载时会延迟甚至丢弃任务，而整点正是高负载时段。
- **并发**：`concurrency` 保证同一时间只有一次运行，避免手动触发与定时任务撞车导致重复续期。
- **续期窗口**：DigitalPlat 规定距到期 120 天以外不允许续期。脚本对「未到窗口」按正常结果处理（不标红），不会误报失败。
- **Actions 可能被停用**：公开仓库 60 天无提交后 GitHub 会自动停用定时工作流。本项目的 `keepalive` 步骤每次运行都会提交一个时间戳，因此不会触发停用；但如果仓库长期无人访问，仍建议偶尔手动 Run 一次。
- **GitHub 免费额度**：公开仓库的 Actions 不计费；私有仓库每月 2000 分钟，本工作流单次约 1 分钟。

## 🔧 本地运行

```bash
pip install -r requirements.txt

export DIGITALPLAT_API_KEY=dp_live_xxx

# 只检查不续期（推荐先跑这个）
python renew_domains.py --dry-run

# 正式续期
python renew_domains.py
```

常用参数：

| 参数 | 说明 |
| --- | --- |
| `--dry-run` | 只检查不提交 |
| `--threshold-days 90` | 覆盖续期阈值 |
| `--only a.us.kg,b.us.kg` | 只处理指定域名 |

## ⚠️ 安全建议

- **不要提交密钥**：`.env`、`key.txt` 已在 `.gitignore` 中排除。
- **定期轮换**：在 Dashboard 里重新生成 API Key，并同步更新 Secret。
- **最小权限**：只给这个 Key 域名读写权限，不要用主账号的长期凭据。
- **日志脱敏**：脚本会把 `dp_live_…`、Telegram Bot Token 替换为 `<已脱敏>`，报错信息可以安全贴到 issue。

## 📚 文档

- [docs/API.md](docs/API.md) —— 本项目用到的 DigitalPlat One API 端点、请求/响应结构、错误处理策略
- [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) —— 常见报错与排查
