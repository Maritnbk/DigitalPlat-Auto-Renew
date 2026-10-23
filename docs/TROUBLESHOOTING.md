# 排查指南

## 工作流没有按时运行

1. **Fork 后定时任务默认关闭**。去 Actions 页面点 **Enable workflow**。
2. **公开仓库 60 天无提交会被停用 schedule**。本项目每次运行都会提交 `.keepalive`，
   正常情况下不会触发；若已被停用，进 Actions 页面重新启用即可。
3. **cron 是 UTC**。`23 0 1 * *` = 每月 1 日 UTC 00:23 = 北京时间 08:23。
4. GitHub 的 `schedule` 在高峰期会延迟，甚至直接跳过某次运行——这是平台行为，不是脚本问题。

## `❌ 账户 1: 获取域名列表失败 —— Invalid API key [invalid_api_key]`

- Key 复制错了（少字符、带了空格、带了引号）。
- Key 已被删除或轮换：去 Dashboard → API Keys 重新创建。
- 用了 `dp_test_` 的沙箱 Key 访问生产环境。
- Secret 里放了多个 Key 但用了中文逗号：脚本按 `[\s,;]+` 切分，中文逗号不会被识别，请改用英文逗号或换行。

## `❌ ... 续期失败（... [invalid_payment_method]）`

请求体里的 `payment_method` 取值不被接受。本项目默认**不发送**该字段；
如果你在 Variables 里配了 `DIGITALPLAT_PAYMENT_METHOD`，先删掉它再跑一次。

## `⏭️ ... 无需续期或尚未进入续期窗口`

这是**正常结果**，不是错误：

- 域名距到期还超过 120 天，DigitalPlat 不允许续期。
- 域名是 `lifecycle_type=permanent` 的永久域名。
- 域名不是注册商域名（`registered_domain_not_found`），由子域名体系管理，本身不需要续期。

如果域名明明快到期却一直显示「未进入窗口」，检查 `DIGITALPLAT_TZ_OFFSET`：
脚本按该偏移把到期日换算成当地日期再算剩余天数，偏移错了剩余天数会整体偏移。

## `⚠️ 接口返回成功，但到期时间未变化`

续期接口返回 `success: true`，但复核列表时到期日没变。可能原因：

- 服务端异步处理，到期日延迟更新。
- 该域名走的是子域名体系，续期接口返回成功但实际没有可续的注册期。

报告里会标黄，建议隔天在 Dashboard 上人工确认一次。脚本不会因此把工作流标红，
但会明确写出来，不会假装成功。

## `❌ ... 域名处于 pendingdelete（待释放）`

域名已经被 `DELETE /domains/{domain}` 移入待释放状态，7 天后释放，期间 DNS 已停用。
这属于账号侧的操作结果，脚本无法续期，需要人工决定是否重新注册。

## 推送没收到

1. **先看 Actions 日志和 Job Summary** —— 即使所有通知通道都失败，结果也在那里。
2. Telegram：
   - 必须先给 Bot 发过一条消息，Bot 才能给你推送。
   - `TELEGRAM_CHAT_ID` 可以通过 `https://api.telegram.org/bot<TOKEN>/getUpdates` 获取。
   - 群组 ID 是负数（如 `-1001234567890`）。
3. PushPlus：
   - `code=999` 是频率限制（1 分钟 5 次 / 相同内容 1 小时 3 条）。报告首行带时间戳，正常不会触发。
   - `code=905` 表示账号未实名。
4. **只要有一个通道送达就算成功**；全部失败才会让工作流标红。

## 工作流标红（exit code 1）

以下情况会让退出码为 1：

- 缺少 `DIGITALPLAT_API_KEY`（手动触发时）。
- 拉取域名列表失败（Key 无效、网络、服务端 5xx）。
- 某个域名续期返回真正的失败（非「未到窗口」类）。
- 所有通知通道都推送失败。
- 域名记录缺少 `domain` 字段。

「未到窗口」「永久域名」「非注册商域名」都**不会**导致标红。

## 想先验证再正式跑

```bash
# 只检查不续期
DIGITALPLAT_API_KEY=dp_live_xxx python renew_domains.py --dry-run

# 只处理一个域名
DIGITALPLAT_API_KEY=dp_live_xxx python renew_domains.py --only your.us.kg
```

在 Actions 页面手动 Run 时也可以勾选 `dry_run`。

## 日志里会不会泄漏 Key？

不会。脚本的 `redact()` 会把 `dp_live_…` / `dp_test_…`、Telegram Bot Token 替换为 `<已脱敏>`，
GitHub 注解（`::error::`）也会先脱敏再输出。但 GitHub 自身的 secret masking 是最后一道防线，
**不要把 Key 直接 echo 到日志里**。
