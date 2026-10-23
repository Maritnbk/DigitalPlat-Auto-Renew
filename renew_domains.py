#!/usr/bin/env python3
"""DigitalPlat-Auto-Renew —— DigitalPlat 免费域名自动续期。

接口行为依据官方文档《DigitalPlat One API Documentation》（见 docs/API.md）:
    GET  /api/v1/domains                  列出账号下所有域名
    POST /api/v1/domains/{domain}/renew   续期注册域名

设计原则：
1. 任何「不确定」都必须在报告里显式暴露，绝不静默跳过；
2. 续期是有副作用的写操作，只在确定安全时重试；
3. 免费续期失败要让人看得见原因，而不是只剩一句 HTTP 401。
"""

import argparse
import logging
import os
import re
import sys
import time
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

DEFAULT_BASE_URL = "https://domain-api.digitalplat.org/api/v1"

# 官方文档未写明日志窗口长度；Dashboard 与多个实现均以「距到期 120 天内可续期」
# 为准（超出时提示 more than 120 days）。阈值取 120，可用变量覆盖。
DEFAULT_THRESHOLD_DAYS = 120
DEFAULT_RENEWAL_YEARS = 1
DEFAULT_MIN_INTERVAL = 1.5
DEFAULT_TZ_OFFSET_HOURS = 8.0

REQUEST_TIMEOUT = (10, 45)
USER_AGENT = "DigitalPlat-Auto-Renew/1.0 (+https://github.com/)"

PUSHPLUS_URL = "https://www.pushplus.plus/send"
TELEGRAM_API_BASE = "https://api.telegram.org"
TELEGRAM_MESSAGE_LIMIT = 4096

# PushPlus 返回码，摘自官方文档 https://pushplus.plus/doc/guide/code.html
PUSHPLUS_CODE_HINTS = {
    900: "当日请求次数已用尽，次日凌晨恢复；请勿继续请求，否则可能延长封禁",
    903: "accessKey 无效",
    905: "账号未实名认证，无法调用发送接口",
    999: "常见于推送频率过快（1 分钟 5 次 / 相同内容 1 小时 3 条），请降低频率后重试",
}

# 视为「域名正常」的状态。官方文档示例是 "ok"，实测还有 "active"。
NORMAL_STATUSES = frozenset({"ok", "active", "registered", "live"})

# 视为「永久有效、无需续期」的生命周期类型。
PERMANENT_LIFECYCLES = frozenset({"permanent", "forever", "never"})

# 这些错误码表示「本次不该续期」，属于预期结果而非失败：
# - 未进入续期窗口
# - 域名并非注册商域名（由 DigitalPlat 子域名体系管理，无需续期）
# - 永久域名
BENIGN_CODES = frozenset({
    "renewal_not_yet_available",
    "renewal_window_not_open",
    "not_in_renewal_window",
    "renew_too_early",
    "too_early",
    "renewal_too_early",
    "registered_domain_not_found",
    "domain_permanent",
    "permanent_domain",
    "no_renewal_required",
})

# 官方未给出错误码清单时，用消息文本兜底识别「未到窗口」这类正常结果。
BENIGN_MESSAGE_PATTERNS = (
    r"more than \d+ days",
    r"cannot (?:be )?renew(?:ed)? (?:yet|before)",
    r"not (?:yet )?(?:in|within) (?:the )?renewal",
    r"renewal (?:window|period).*(?:not|has not|opens?)",
    r"too early",
    r"not (?:yet )?available",
    r"permanent",
    r"no renewal (?:is )?required",
)

# 官方错误码 -> 用户下一步该做什么
ERROR_HINTS = {
    "invalid_api_key": "API Key 无效，请到 DigitalPlat 后台 API Keys 页面重新创建",
    "unauthorized": "API Key 无效或已被吊销",
    "forbidden": "当前 API Key 无权访问该资源",
    "insufficient_scope": "API Key 权限不足，请重新创建包含域名读写权限的 Key",
    "rate_limit_exceeded": "触发接口频率限制，请调大 DIGITALPLAT_MIN_INTERVAL 或稍后重试",
    "quota_exceeded": "账号额度不足，无法续期",
    "insufficient_credits": "账号积分/额度不足，付费续期将失败",
    "registered_domain_not_found": "该域名不是注册商域名（由子域名体系管理），无需也无法通过 API 续期",
    "domain_not_found": "域名不存在或不属于当前 API Key",
    "invalid_payment_method": "payment_method 取值不被接受，请检查 DIGITALPLAT_PAYMENT_METHOD",
    "payment_method_not_supported": "当前环境不支持该支付方式，请调整 DIGITALPLAT_PAYMENT_METHOD",
    "internal_error": "DigitalPlat 服务端内部错误，稍后重试",
    "service_unavailable": "DigitalPlat 服务暂时不可用，稍后重试",
}

_TRUE_WORDS = frozenset({"1", "true", "yes", "y", "on", "t"})
_FALSE_WORDS = frozenset({"0", "false", "no", "n", "off", "f", "", "none", "null"})


class ConfigError(RuntimeError):
    """配置缺失或非法。"""


class ApiError(RuntimeError):
    """接口调用失败（含响应结构不符合预期）。"""

    def __init__(self, message, code=None, status=None, payload=None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.payload = payload


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #

def configure_streams():
    """报告含 emoji，非 UTF-8 终端（Windows GBK 等）下 print 会抛 UnicodeEncodeError。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def warn(message):
    logging.getLogger("renew").warning(message)


def to_bool(value, default=False, label=""):
    """稳妥地把接口返回值转成布尔。

    PHP 后端把 tinyint 序列化成字符串是常见行为，而 bool("0") is True ——
    对 never_expires / permanent 这类字段误判会静默跳过续期，是最坏的失效方式。
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value).strip().lower()
    if text in _TRUE_WORDS:
        return True
    if text in _FALSE_WORDS:
        return False
    warn(f"{label or '布尔字段'}={value!r} 无法识别，按 {default} 处理")
    return default


def parse_tz_offset(raw, default_hours=DEFAULT_TZ_OFFSET_HOURS):
    """解析形如 '+8' / '8' / '-5' / '+5.5' 的时区偏移，非法值回退到默认。"""
    if raw is None or not str(raw).strip():
        return timezone(timedelta(hours=default_hours))
    try:
        hours = float(str(raw).strip())
    except ValueError:
        warn(f"DIGITALPLAT_TZ_OFFSET={raw!r} 不是合法数字，回退到 UTC+{default_hours:g}")
        return timezone(timedelta(hours=default_hours))
    if not -24 < hours < 24:
        warn(f"DIGITALPLAT_TZ_OFFSET={raw!r} 超出范围，回退到 UTC+{default_hours:g}")
        return timezone(timedelta(hours=default_hours))
    return timezone(timedelta(hours=hours))


def format_offset(tz):
    """把 tzinfo 渲染成 'UTC+8' 这类可读文本。"""
    total_minutes = int(tz.utcoffset(None).total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    hours, minutes = divmod(abs(total_minutes), 60)
    return f"UTC{sign}{hours}" + (f":{minutes:02d}" if minutes else "")


def env_number(env, name, default, cast):
    raw = env.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return cast(str(raw).strip())
    except ValueError:
        warn(f"{name}={raw!r} 不是合法数值，回退到默认值 {default}")
        return default


def split_keys(raw):
    """支持一个 Secret 里放多个 API Key：换行或逗号分隔。"""
    if not raw:
        return []
    parts = re.split(r"[\s,;]+", str(raw).strip())
    return [p for p in parts if p]


def redact(text, secrets=()):
    """日志脱敏：公开仓库的 Actions 日志人人可见，密钥绝不能出现。"""
    result = str(text)
    for secret in secrets:
        if secret and len(secret) >= 8:
            result = result.replace(secret, "<已脱敏>")
    result = re.sub(r"(dp_(?:live|test)_)[A-Za-z0-9_\-]+", r"\1<已脱敏>", result)
    result = re.sub(r"/bot[^/\s]+/", "/bot<已脱敏>/", result)
    return result


def gh_annotation(level, message):
    """在 GitHub Actions 日志里生成可点击的注解。"""
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return
    text = redact(str(message).replace("\n", " ").replace("%", "%25")
                  .replace("\r", "%0D"), _all_secrets())
    print(f"::{level}::{text}")


_SECRET_CACHE: List[str] = []


def _all_secrets():
    if not _SECRET_CACHE:
        for name in ("DIGITALPLAT_API_KEY", "TG_BOT_TOKEN", "PUSHPLUS_TOKEN",
                     "TELEGRAM_BOT_TOKEN"):
            _SECRET_CACHE.extend(split_keys(os.environ.get(name)))
    return _SECRET_CACHE


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Config:
    api_keys: Tuple[str, ...] = ()
    base_url: str = DEFAULT_BASE_URL
    threshold_days: int = DEFAULT_THRESHOLD_DAYS
    renewal_years: int = DEFAULT_RENEWAL_YEARS
    payment_method: str = ""
    min_interval: float = DEFAULT_MIN_INTERVAL
    tz: timezone = field(default_factory=lambda: timezone(timedelta(hours=DEFAULT_TZ_OFFSET_HOURS)))
    dry_run: bool = False
    only: Tuple[str, ...] = ()
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    pushplus_token: str = ""
    pushplus_topic: str = ""

    @classmethod
    def from_env(cls, env=None, args=None):
        env = os.environ if env is None else env
        args = args or argparse.Namespace()

        def text(name):
            return (env.get(name) or "").strip()

        threshold = getattr(args, "threshold_days", None)
        if threshold is None:
            threshold = env_number(env, "DIGITALPLAT_THRESHOLD_DAYS",
                                   DEFAULT_THRESHOLD_DAYS, int)

        only = tuple(d.lower() for d in split_keys(
            getattr(args, "only", "") or text("DIGITALPLAT_ONLY")))

        return cls(
            api_keys=tuple(split_keys(text("DIGITALPLAT_API_KEY"))),
            base_url=text("DIGITALPLAT_API_BASE_URL") or DEFAULT_BASE_URL,
            threshold_days=max(0, threshold),
            renewal_years=max(1, env_number(env, "DIGITALPLAT_RENEWAL_YEARS",
                                            DEFAULT_RENEWAL_YEARS, int)),
            payment_method=text("DIGITALPLAT_PAYMENT_METHOD"),
            min_interval=max(0.0, env_number(env, "DIGITALPLAT_MIN_INTERVAL",
                                             DEFAULT_MIN_INTERVAL, float)),
            tz=parse_tz_offset(env.get("DIGITALPLAT_TZ_OFFSET")),
            dry_run=bool(getattr(args, "dry_run", False))
            or to_bool(text("DIGITALPLAT_DRY_RUN") or "false", label="DIGITALPLAT_DRY_RUN"),
            only=only,
            telegram_bot_token=text("TELEGRAM_BOT_TOKEN") or text("TG_BOT_TOKEN"),
            telegram_chat_id=text("TELEGRAM_CHAT_ID") or text("TG_CHAT_ID"),
            pushplus_token=text("PUSHPLUS_TOKEN"),
            pushplus_topic=text("PUSHPLUS_TOPIC"),
        )

    def missing_credentials(self):
        return [] if self.api_keys else ["DIGITALPLAT_API_KEY"]


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

class Throttle:
    """保证相邻两次调用间隔不小于 min_interval，避免撞限流。"""

    def __init__(self, min_interval, sleep=time.sleep, clock=time.monotonic):
        self.min_interval = min_interval
        self._sleep = sleep
        self._clock = clock
        self._last = None

    def wait(self):
        if self.min_interval <= 0:
            return
        if self._last is not None:
            remaining = self.min_interval - (self._clock() - self._last)
            if remaining > 0:
                self._sleep(remaining)
        self._last = self._clock()


def read_retry():
    """只读请求可以放心对连接错误、读超时、5xx、429 全面重试。"""
    return Retry(total=4, connect=3, read=3, status=3,
                 status_forcelist=(429, 500, 502, 503, 504),
                 allowed_methods=frozenset(["GET"]),
                 backoff_factor=2, respect_retry_after_header=True)


def renew_retry():
    """续期有副作用且官方未声明幂等保证，只重试确定没有产生副作用的情形。

    connect 错误发生在请求发出之前；429 是服务端明确拒收。两者都安全。
    读超时和 5xx 不重试 —— 服务端可能已经续期成功，重试会重复下单。
    """
    return Retry(total=2, connect=2, read=0, status=1,
                 status_forcelist=(429,),
                 allowed_methods=frozenset(["POST"]),
                 backoff_factor=2, respect_retry_after_header=True)


def notify_retry():
    """重复推送只是轻微骚扰，收不到通知才是真问题，所以推送重试更激进。"""
    return Retry(total=3, connect=2, read=2, status=2,
                 status_forcelist=(429, 500, 502, 503, 504),
                 allowed_methods=frozenset(["POST"]),
                 backoff_factor=1, respect_retry_after_header=True)


def build_session(retry):
    session = requests.Session()
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    })
    return session


def error_parts(payload):
    """从多种错误结构中取出 (code, message, details)。

    实测线上 401 返回 {"error": {"code": ..., "message": ...}, "success": false}，
    而文档示例是扁平结构，两种都要兼容。
    """
    if not isinstance(payload, dict):
        return None, None, {}
    details = payload.get("details") if isinstance(payload.get("details"), dict) else {}
    error = payload.get("error")
    if isinstance(error, dict):
        return (error.get("code") or payload.get("error_code"),
                error.get("message") or payload.get("message"),
                {**details, **{k: v for k, v in error.items()
                               if k not in ("code", "message")}})
    if isinstance(error, str) and error:
        return payload.get("error_code"), error, details
    return (payload.get("error_code"),
            payload.get("message") or payload.get("msg"),
            details)


def is_failure_payload(payload):
    if not isinstance(payload, dict):
        return True
    if payload.get("success") is False:
        return True
    code, message, _ = error_parts(payload)
    return bool(code or (message and not payload.get("success")))


def describe_error(payload, fallback="未知错误"):
    """按统一结构拼出可读信息：message [code] (details)。"""
    if not isinstance(payload, dict):
        return fallback

    code, message, details = error_parts(payload)
    text = str(message or fallback)
    if code:
        text += f" [{code}]"

    extra = [f"{k}={details[k]}" for k in ("limit", "remaining", "reset_at", "retry_after")
             if details.get(k) is not None]
    if extra:
        text += f" ({', '.join(extra)})"

    days = details.get("days_until_window")
    if days is not None:
        text += f"，还需等待 {days} 天"
    elif details.get("seconds_until_window") is not None:
        text += f"，还需等待约 {max(1, int(details['seconds_until_window']) // 86400)} 天"

    hint = ERROR_HINTS.get(str(code).lower()) if code else None
    if hint:
        text += f" —— {hint}"
    return text


def is_benign(payload):
    """判断是否为「本次不该续期」的预期结果。"""
    if not isinstance(payload, dict):
        return False
    code, message, _ = error_parts(payload)
    if code and str(code).strip().lower() in BENIGN_CODES:
        return True
    text = str(message or "").lower()
    return any(re.search(pattern, text) for pattern in BENIGN_MESSAGE_PATTERNS)


# --------------------------------------------------------------------------- #
# 客户端
# --------------------------------------------------------------------------- #

class DigitalPlatClient:
    def __init__(self, config, throttle=None, read_session=None, write_session=None):
        self.config = config
        self.throttle = throttle or Throttle(config.min_interval)
        self.read_session = read_session or build_session(read_retry())
        self.write_session = write_session or build_session(renew_retry())

    def _headers(self, api_key, extra=None):
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        if extra:
            headers.update(extra)
        return headers

    def _url(self, path):
        return f"{self.config.base_url.rstrip('/')}{path}"

    @staticmethod
    def _payload(resp, what):
        try:
            data = resp.json()
        except ValueError:
            snippet = redact((resp.text or "")[:300])
            raise ApiError(f"{what}返回非 JSON 内容 (HTTP {resp.status_code}): {snippet}",
                           status=resp.status_code)
        if not isinstance(data, dict):
            raise ApiError(f"{what}响应格式异常: {str(data)[:200]}", status=resp.status_code)
        return data

    def _read(self, api_key, path, what):
        self.throttle.wait()
        try:
            resp = self.read_session.get(self._url(path), headers=self._headers(api_key),
                                         timeout=REQUEST_TIMEOUT)
        except requests.RequestException as exc:
            raise ApiError(f"{what}网络异常: {redact(exc)}")
        return self._checked(resp, what)

    def _checked(self, resp, what):
        """统一处理响应：body 里的错误结构优先于 HTTP 状态码。

        不能先 raise_for_status：那样只剩「401 Client Error」，
        把 invalid_api_key / rate_limit_exceeded 这类可行动信息全丢掉。
        """
        try:
            data = self._payload(resp, what)
        except ApiError as parse_error:
            if resp.status_code >= 400:
                snippet = redact((resp.text or "")[:300] or "空响应")
                raise ApiError(
                    f"{what}失败 (HTTP {resp.status_code}): {snippet}",
                    status=resp.status_code,
                )
            raise parse_error

        if is_failure_payload(data):
            code, _, _ = error_parts(data)
            raise ApiError(f"{what}返回失败: {describe_error(data)}",
                           code=code, status=resp.status_code, payload=data)

        if resp.status_code >= 400:
            raise ApiError(f"{what}失败 (HTTP {resp.status_code}): {str(data)[:200]}",
                           status=resp.status_code, payload=data)
        return data

    # -- 业务方法 ---------------------------------------------------------- #

    def list_domains(self, api_key):
        data = self._read(api_key, "/domains", "域名列表")
        domains = data.get("data")
        if isinstance(domains, dict):
            # 兼容 {data: {domains: [...]}} 这种再包一层的结构
            domains = domains.get("domains", domains.get("items"))
        if domains is None:
            # 兼容旧结构 {domains: [...]}
            domains = data.get("domains")
        if domains is None:
            raise ApiError("域名列表响应缺少 data 字段（接口可能已变更），"
                           f"原始响应: {str(data)[:200]}")
        if not isinstance(domains, list):
            raise ApiError(f"域名列表的 data 字段不是数组: {str(domains)[:200]}")
        return [item for item in domains if isinstance(item, dict)]

    def renew_domain(self, api_key, domain, years=None, payment_method=None):
        """提交续期。返回响应体（可能是业务失败），仅传输层/HTTP 层问题抛异常。

        续期的 4xx（未到窗口等）是预期内的业务结果，要原样交给调用方渲染。
        """
        years = self.config.renewal_years if years is None else years
        payload: Dict[str, Any] = {"years": years}
        method = self.config.payment_method if payment_method is None else payment_method
        if method:
            payload["payment_method"] = method

        path = f"/domains/{requests.utils.quote(domain, safe='')}/renew"
        self.throttle.wait()
        try:
            resp = self.write_session.post(
                self._url(path),
                headers=self._headers(api_key, {"Idempotency-Key": str(uuid.uuid4())}),
                json=payload,
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise ApiError(f"续期请求网络异常: {redact(exc)}")

        try:
            data = self._payload(resp, "续期接口")
        except ApiError as parse_error:
            if resp.status_code >= 400:
                snippet = redact((resp.text or "")[:300] or "空响应")
                raise ApiError(f"续期接口失败 (HTTP {resp.status_code}): {snippet}",
                               status=resp.status_code)
            raise parse_error

        if resp.status_code >= 500 and not is_failure_payload(data):
            # 5xx 却给出成功结构，属于自相矛盾，按失败处理，避免误报续期成功
            raise ApiError(f"续期接口异常 (HTTP {resp.status_code}): {str(data)[:200]}",
                           status=resp.status_code, payload=data)
        return data, payload


# --------------------------------------------------------------------------- #
# 域名视图与判定
# --------------------------------------------------------------------------- #

def parse_expiry(value, tz):
    """解析到期时间，返回 date；无法解析返回 None（不抛异常）。

    文档示例是纯日期 "2027-04-08"，实测可能带 ISO 时间戳。
    纯日期按「当天结束」理解，但对剩余天数没有影响。
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e11:
            ts /= 1000.0
        return datetime.fromtimestamp(ts, tz=tz).date()

    text = str(value).strip()
    if not text or text.lower() in ("null", "none", "permanent", "never"):
        return None

    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        try:
            return date.fromisoformat(text)
        except ValueError:
            return None

    if re.fullmatch(r"\d{8}", text):
        try:
            return datetime.strptime(text, "%Y%m%d").date()
        except ValueError:
            return None

    iso = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(iso)
    except ValueError:
        warn(f"无法解析到期时间 {value!r}")
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    return parsed.astimezone(tz).date()


@dataclass
class DomainView:
    """一条域名记录的规范化视图，便于纯函数判定与测试。"""

    label: str
    account: int = 1
    domain: str = ""
    status: Optional[str] = None
    slot_type: Optional[str] = None
    domain_kind: Optional[str] = None
    lifecycle_type: Optional[str] = None
    expires_raw: Optional[str] = None
    expires_on: Optional[date] = None
    days_remaining: Optional[int] = None
    problem: Optional[str] = None

    @property
    def status_abnormal(self):
        if not self.status:
            return None
        if str(self.status).strip().lower() in NORMAL_STATUSES:
            return None
        return str(self.status)

    @property
    def permanent(self):
        """是否永久有效（即没有到期日）。

        实测 DigitalPlat 会对平台自营子域名（domain_kind=operated，如 dpdns.org）
        同时返回 lifecycle_type="permanent" 和一个真实到期日（如 "20270419"）。
        只凭 lifecycle_type 判永久，会把「还剩 178 天到期」的域名说成「永久有效」，
        既不符合事实，也把到期日藏了起来。所以以「有没有可解析的到期日」为准：
        有到期日就按到期日算，没有才认定永久。
        """
        if self.expires_on is not None:
            return False
        lifecycle = str(self.lifecycle_type or "").strip().lower()
        if lifecycle in PERMANENT_LIFECYCLES:
            return True
        raw = str(self.expires_raw or "").strip().lower()
        return raw in ("permanent", "never")

    @property
    def platform_operated(self):
        """平台自营子域名（domain_kind=operated）。

        这类域名不走 registrar 续期接口：实测 POST /domains/{domain}/renew
        返回 404 registered_domain_not_found。所以报告里要标出来，
        避免用户以为「脚本漏续了」。
        """
        return str(self.domain_kind or "").strip().lower() == "operated"

    @property
    def kind_note(self):
        return "，平台自营子域名" if self.platform_operated else ""

    @property
    def pending_delete(self):
        return str(self.status or "").strip().lower() in ("pendingdelete", "pending_delete")


def normalize_domain(raw, account, index, tz, today):
    position = f"<第{index + 1}条记录>"
    if not isinstance(raw, dict):
        return DomainView(label=position, account=account,
                          problem=f"记录格式异常: {str(raw)[:80]}")

    domain = str(raw.get("domain") or raw.get("name") or "").strip()
    label = domain or position
    view = DomainView(
        label=label,
        account=account,
        domain=domain,
        status=raw.get("status"),
        slot_type=raw.get("slot_type"),
        domain_kind=raw.get("domain_kind"),
        lifecycle_type=raw.get("lifecycle_type"),
        expires_raw=raw.get("expires_at") or raw.get("expiry_date"),
    )
    if not domain:
        view.problem = "响应缺少 domain 字段，无法续期"
        return view

    view.expires_on = parse_expiry(view.expires_raw, tz)
    if view.expires_on is not None:
        view.days_remaining = (view.expires_on - today).days
    return view


def decide(view, threshold_days):
    """判定本次要做什么：invalid / permanent / pendingdelete / skip / renew。

    边界按「剩余天数 <= 阈值」判定：官方窗口是距到期 120 天内，
    正好 120 天时窗口已经打开，不能判成跳过。
    """
    if view.problem:
        return "invalid"
    # pendingdelete 优先于 permanent：域名已被删除进入待释放状态，
    # 即使没有到期日也要让人看见「它正在被释放」，不能报成永久有效。
    if view.pending_delete:
        return "pendingdelete"
    if view.permanent:
        return "permanent"
    if view.days_remaining is None:
        # 到期时间未知：宁可多撞一次「未到窗口」，也不要漏掉真要过期的域名
        return "renew"
    if view.days_remaining > threshold_days:
        return "skip"
    return "renew"


def describe_remaining(view):
    if view.days_remaining is None:
        return None
    if view.days_remaining < 0:
        return f"已过期 {-view.days_remaining} 天"
    return f"剩余 {view.days_remaining} 天"


def status_note(view):
    abnormal = view.status_abnormal
    if not abnormal:
        return ""
    if abnormal.lower() in ("expired", "pendingdelete", "pending_delete"):
        return f"  ⚠️ 域名状态 {abnormal}（可能已无法正常续期）"
    return f"  ⚠️ 域名状态 {abnormal}"


def expiry_line(view):
    if view.problem:
        return f"{view.label}: 到期时间 未知（{view.problem}）{status_note(view)}"
    if view.permanent:
        return f"{view.label}: 永久有效（无需续期）{status_note(view)}"
    remaining = describe_remaining(view)
    if view.expires_on is not None:
        text = f"{view.label}: 到期 {view.expires_on.isoformat()}"
        if remaining:
            text += f"（{remaining}{view.kind_note}）"
        return text + status_note(view)
    if view.expires_raw:
        return f"{view.label}: 到期 {view.expires_raw}（无法解析剩余天数）{status_note(view)}"
    return f"{view.label}: 到期时间 未知{status_note(view)}"


@dataclass
class RenewOutcome:
    line: str
    failed: bool
    benign: bool = False
    new_expiry: Optional[date] = None


# --------------------------------------------------------------------------- #
# 通知
# --------------------------------------------------------------------------- #

def utf16_len(text):
    """Telegram 的 4096 上限按 UTF-16 码元计，emoji 占 2 个，用 len() 会低估。"""
    return len(text.encode("utf-16-le")) // 2


def _utf16_prefix_len(text, limit):
    used = 0
    for index, char in enumerate(text):
        used += 2 if ord(char) > 0xFFFF else 1
        if used > limit:
            return index
    return len(text)


def split_message(content, limit=TELEGRAM_MESSAGE_LIMIT):
    """超长报告按 UTF-16 码元切分，优先在换行处断开，避免把域名切成两半。"""
    chunks = []
    remaining = content
    while utf16_len(remaining) > limit:
        hard = _utf16_prefix_len(remaining, limit)
        cut = remaining.rfind("\n", 0, hard + 1)
        if cut <= 0:
            cut = hard
        chunks.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    chunks.append(remaining)
    return chunks


class TelegramNotifier:
    """纯文本发送，超长报告自动分段。

    不用 parse_mode：报告含 emoji、域名和接口错误原文，Markdown 转义易漏且无收益。
    """

    def __init__(self, bot_token, chat_id, session=None):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.session = session or build_session(notify_retry())

    def send(self, content):
        if not self.bot_token or not self.chat_id:
            return True
        url = f"{TELEGRAM_API_BASE}/bot{self.bot_token}/sendMessage"
        ok = True
        for index, chunk in enumerate(split_message(content), start=1):
            try:
                resp = self.session.post(url, json={"chat_id": self.chat_id, "text": chunk},
                                         timeout=REQUEST_TIMEOUT)
                if resp.status_code >= 400:
                    detail = redact((resp.text or "")[:200], (self.bot_token,))
                    warn(f"Telegram 第 {index} 段推送失败 (HTTP {resp.status_code}): {detail}")
                    ok = False
                    continue
                result = resp.json()
                if not isinstance(result, dict) or not result.get("ok"):
                    warn(f"Telegram 第 {index} 段推送未成功: "
                         f"{redact(str(result)[:200], (self.bot_token,))}")
                    ok = False
            except Exception as exc:  # noqa: BLE001 - 通知失败不能吞掉主流程
                warn(f"Telegram 第 {index} 段推送失败: {redact(exc, (self.bot_token,))}")
                ok = False
        if ok:
            print("Telegram 推送成功")
        return ok


class PushPlusNotifier:
    def __init__(self, token, topic="", session=None):
        self.token = token
        self.topic = topic
        self.session = session or build_session(notify_retry())

    def send(self, content):
        if not self.token:
            return True
        data = {
            "token": self.token,
            "title": "DigitalPlat-Auto-Renew 续期报告",
            "content": content,
            "template": "txt",
            "topic": self.topic,
        }
        try:
            resp = self.session.post(PUSHPLUS_URL, json=data, timeout=REQUEST_TIMEOUT)
            try:
                result = resp.json()
            except ValueError:
                warn(f"PushPlus 返回非 JSON 内容: {redact((resp.text or '')[:200])}")
                return False
            if not isinstance(result, dict):
                warn(f"PushPlus 响应格式异常: {str(result)[:200]}")
                return False
            if result.get("code") != 200:
                parts = [f"PushPlus 推送未成功 (code={result.get('code')}): {result.get('msg')}"]
                if result.get("data"):
                    parts.append(f"原因: {result['data']}")
                hint = PUSHPLUS_CODE_HINTS.get(result.get("code"))
                if hint:
                    parts.append(hint)
                warn("；".join(parts))
                return False
            print("PushPlus 推送成功")
            return True
        except Exception as exc:  # noqa: BLE001
            warn(f"PushPlus 推送失败: {exc}")
            return False


class CompositeNotifier:
    """任一通道送达即视为成功。

    不能用 all(...)：它会短路，PushPlus 挂掉时 Telegram 根本不会被调用，
    而那恰恰是最需要备份通道的时刻。
    """

    def __init__(self, channels):
        self.channels = channels

    def send(self, content):
        if not self.channels:
            print("未配置任何通知通道，跳过推送（结果已写入 Actions 日志与 Summary）")
            return True
        results = [channel.send(content) for channel in self.channels]
        if not any(results):
            warn(f"所有 {len(results)} 个通知通道均推送失败")
        elif not all(results):
            warn("部分通知通道推送失败，但报告已通过其余通道送达")
        return any(results)


def build_notifier(config):
    channels = []
    if config.telegram_bot_token and config.telegram_chat_id:
        channels.append(TelegramNotifier(config.telegram_bot_token, config.telegram_chat_id))
    elif config.telegram_bot_token or config.telegram_chat_id:
        warn("TELEGRAM_BOT_TOKEN 与 TELEGRAM_CHAT_ID 需同时配置，已忽略不完整的 Telegram 配置")
    if config.pushplus_token:
        channels.append(PushPlusNotifier(config.pushplus_token, config.pushplus_topic))
    return CompositeNotifier(channels)


# --------------------------------------------------------------------------- #
# 报告
# --------------------------------------------------------------------------- #

def build_message(config, started_at, accounts, renewal_lines, expiry_lines,
                  has_failure, dry_run=False):
    parts = []
    parts.append(f"运行时间：{started_at.astimezone(config.tz).strftime('%Y-%m-%d %H:%M:%S')} "
                 f"({format_offset(config.tz)})")
    if dry_run:
        parts.append("模式：DRY-RUN（只检查不续期）")
    parts.append("")

    for account in accounts:
        if len(config.api_keys) > 1:
            parts.append(f"账户 {account['index']}：{account['summary']}")
        else:
            parts.append(account["summary"])
    parts.append("")

    parts.append("=== 本次续期结果 ===")
    if renewal_lines:
        parts.extend(renewal_lines)
    else:
        parts.append(f"（没有需要续期的域名：剩余天数均 > {config.threshold_days} 天）")

    if has_failure:
        parts.append("")
        parts.append("⚠️ 存在失败项，请查看 GitHub Actions 日志并人工处理")

    parts.append("")
    parts.append("=== 所有域名到期时间 ===")
    parts.extend([expiry_line(v) for v in expiry_lines] or ["（无）"])
    parts.append("")
    parts.append(f"（共 {len(expiry_lines)} 个域名；续期阈值 {config.threshold_days} 天；"
                 f"日期按 {format_offset(config.tz)} 计算）")
    return "\n".join(parts)


def build_summary_markdown(config, started_at, accounts, renewal_lines, expiry_lines,
                           has_failure, dry_run):
    """写入 GitHub Actions Job Summary，无需任何通知通道也能看到结果。"""
    lines = []
    lines.append("## DigitalPlat-Auto-Renew 续期报告")
    lines.append("")
    lines.append(f"- 运行时间：`{started_at.astimezone(config.tz).strftime('%Y-%m-%d %H:%M:%S')}`"
                 f"（{format_offset(config.tz)}）")
    lines.append(f"- 续期阈值：剩余 **{config.threshold_days}** 天以内才续期")
    if dry_run:
        lines.append("- 模式：**DRY-RUN**（只检查，不实际续期）")
    lines.append("")
    for account in accounts:
        lines.append(f"- 账户 {account['index']}：{account['summary']}")
    lines.append("")
    lines.append("### 本次续期结果")
    lines.append("")
    lines.extend(renewal_lines or [f"没有需要续期的域名（剩余天数均 > {config.threshold_days} 天）"])
    lines.append("")
    lines.append("### 所有域名到期时间")
    lines.append("")
    lines.append("| 域名 | 到期时间 | 剩余天数 | 状态 | 槽位 | 类型 |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for view in expiry_lines:
        expires = ("永久" if view.permanent
                   else view.expires_on or view.expires_raw or "未知")
        lines.append(f"| `{view.label}` | {expires} | "
                     f"{'' if view.days_remaining is None else view.days_remaining} | "
                     f"{view.status or '-'} | {view.slot_type or '-'} | "
                     f"{view.domain_kind or '-'} |")
    if has_failure:
        lines.append("")
        lines.append("> ⚠️ 存在失败项，请查看运行日志并人工处理。")
    return "\n".join(lines) + "\n"


def write_step_summary(markdown):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(markdown)
    except OSError as exc:
        warn(f"写入 GitHub Step Summary 失败: {exc}")


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #

def renew_one(client, view, api_key, config, today):
    """续期单个域名，返回 RenewOutcome。"""
    try:
        payload, _sent = client.renew_domain(api_key, view.domain)
    except ApiError as exc:
        return RenewOutcome(f"❌ {view.label}: 请求异常 ({exc})", True)

    if not is_failure_payload(payload):
        new_expiry = None
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        for key in ("expires_at", "expiry_date", "expires_on"):
            if data.get(key):
                new_expiry = parse_expiry(data[key], config.tz)
                break
        if new_expiry is None:
            # 官方响应示例不含新到期时间，回到列表接口复核一次
            try:
                refreshed = client.list_domains(api_key)
                for raw in refreshed:
                    if str(raw.get("domain") or raw.get("name") or "").lower() == view.domain.lower():
                        candidate = normalize_domain(raw, view.account, 0, config.tz, today)
                        if candidate.expires_on:
                            new_expiry = candidate.expires_on
                        break
            except ApiError as exc:
                warn(f"{view.label}: 续期后复核到期时间失败（{exc}）")
        if new_expiry and new_expiry != view.expires_on:
            return RenewOutcome(
                f"✅ {view.label}: 续期成功（{view.expires_on or '未知'} → {new_expiry}）",
                False, new_expiry=new_expiry,
            )
        if new_expiry:
            return RenewOutcome(
                f"⚠️ {view.label}: 接口返回成功，但到期时间未变化（仍为 {new_expiry}），请人工核查",
                False, new_expiry=new_expiry,
            )
        return RenewOutcome(f"✅ {view.label}: 续期成功（接口未返回新到期时间，建议人工确认）", False)

    detail = describe_error(payload)
    if is_benign(payload):
        code, _, _ = error_parts(payload)
        if str(code or "").lower() == "registered_domain_not_found":
            reason = ("平台自营子域名，不通过注册商接口续期"
                      if view.platform_operated
                      else "非注册商域名，不通过注册商接口续期")
        else:
            reason = "无需续期或尚未进入续期窗口"
        return RenewOutcome(f"⏭️ {view.label}: {reason}（{detail}）", False, benign=True)
    return RenewOutcome(f"❌ {view.label}: 续期失败（{detail}）", True)


def run(config, client, notifier, started_at=None, today=None):
    """执行一轮续期，返回进程退出码：0 全部正常，1 存在需人工关注的问题。"""
    started_at = started_at or datetime.now(timezone.utc)
    today = today or started_at.astimezone(config.tz).date()

    accounts: List[Dict[str, Any]] = []
    renewal_lines: List[str] = []
    expiry_lines: List[DomainView] = []
    has_failure = False

    for index, api_key in enumerate(config.api_keys, start=1):
        try:
            raw_domains = client.list_domains(api_key)
        except ApiError as exc:
            has_failure = True
            line = f"❌ 账户 {index}: 获取域名列表失败 —— {exc}"
            renewal_lines.append(line)
            gh_annotation("error", line)
            accounts.append({"index": index, "summary": "获取域名列表失败，本次未处理该账户"})
            continue

        views = []
        for position, raw in enumerate(raw_domains):
            view = normalize_domain(raw, index, position, config.tz, today)
            if config.only and view.domain.lower() not in config.only:
                continue
            views.append(view)

        if config.only and not views:
            warn(f"账户 {index}: 未找到 --only 指定的域名 {', '.join(config.only)}")

        free = sum(1 for v in views if str(v.slot_type or "").lower() == "free")
        paid = len(views) - free
        accounts.append({
            "index": index,
            "summary": f"{len(views)} 个域名（免费槽位 {free} / 付费 {paid}）",
        })

        for view in views:
            expiry_lines.append(view)
            action = decide(view, config.threshold_days)

            if action == "invalid":
                has_failure = True
                line = f"❌ {view.label}: {view.problem}"
                renewal_lines.append(line)
                gh_annotation("error", line)
                continue
            if action == "permanent":
                renewal_lines.append(f"⏭️ {view.label}: 无到期日，按永久有效处理，跳过续期")
                continue
            if action == "pendingdelete":
                has_failure = True
                line = f"❌ {view.label}: 域名处于 pendingdelete（待释放），无法续期"
                renewal_lines.append(line)
                gh_annotation("error", line)
                continue
            if action == "skip":
                kind = "（平台自营子域名）" if view.platform_operated else ""
                renewal_lines.append(
                    f"⏭️ {view.label}: {describe_remaining(view)} > {config.threshold_days} 天，"
                    f"尚未进入续期窗口，跳过{kind}"
                )
                continue

            if config.dry_run:
                renewal_lines.append(
                    f"🔍 {view.label}: 满足续期条件（{describe_remaining(view)}），"
                    f"DRY-RUN 未实际提交"
                )
                continue

            outcome = renew_one(client, view, api_key, config, today)
            renewal_lines.append(outcome.line + status_note(view))
            has_failure = has_failure or outcome.failed
            if outcome.failed:
                gh_annotation("error", outcome.line)
            if outcome.new_expiry:
                view.expires_on = outcome.new_expiry
                view.days_remaining = (outcome.new_expiry - today).days
                view.expires_raw = outcome.new_expiry.isoformat()

    message = build_message(config, started_at, accounts, renewal_lines, expiry_lines,
                            has_failure, config.dry_run)
    print(message)
    write_step_summary(build_summary_markdown(config, started_at, accounts, renewal_lines,
                                              expiry_lines, has_failure, config.dry_run))

    pushed = notifier.send(message)
    return 0 if (not has_failure and pushed) else 1


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="DigitalPlat-Auto-Renew：DigitalPlat 免费域名自动续期")
    parser.add_argument("--dry-run", action="store_true", help="只检查，不实际提交续期")
    parser.add_argument("--threshold-days", type=int, default=None,
                        help=f"剩余天数不超过该值才续期（默认 {DEFAULT_THRESHOLD_DAYS}）")
    parser.add_argument("--only", default="", help="只处理指定域名，逗号分隔")
    return parser.parse_args(argv)


def main(argv=None):
    configure_streams()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    args = parse_args(argv)

    config = Config.from_env(args=args)
    notifier = build_notifier(config)

    missing = config.missing_credentials()
    if missing:
        message = (f"❌ 缺少必需的环境变量: {', '.join(missing)}\n"
                   "请在仓库 Settings -> Secrets and variables -> Actions 中配置后重试。")
        print(message, file=sys.stderr)
        gh_annotation("error", message)
        notifier.send(message)
        return 1

    client = DigitalPlatClient(config)

    if len(config.api_keys) > 1:
        print(f"检测到 {len(config.api_keys)} 个 API Key，将依次处理")

    return run(config, client, notifier)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:
        configure_streams()
        detail = traceback.format_exc()
        print(redact(detail), file=sys.stderr)
        try:
            build_notifier(Config.from_env()).send(
                f"⚠️ 续期脚本异常中断，本次续期未完成\n\n{redact(detail[-1500:])}")
        except Exception:  # noqa: BLE001
            pass
        sys.exit(1)
