"""
DeepSeek 自动搜索/对话插件 v0.5.0。

浏览器生命周期、profile、权限和临时目录全部由
astrbot_plugin_browser_operator 管理；本插件只使用它公开的桥接接口。
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register


DEEPSEEK_URL = "https://chat.deepseek.com"
DEEPSEEK_HOST = "chat.deepseek.com"

ST_INIT_PAGE = "INIT_PAGE"
ST_ENSURE_CHAT = "ENSURE_CHAT"
ST_ENSURE_SWITCHES = "ENSURE_SWITCHES"
ST_FILL_PROMPT = "FILL_PROMPT"
ST_SEND_PROMPT = "SEND_PROMPT"
ST_VERIFY_SENT = "VERIFY_SENT"
ST_WAIT_START = "WAIT_ASSISTANT_START"
ST_WAIT_DONE = "WAIT_ASSISTANT_DONE"
ST_EXTRACT = "EXTRACT_REPLY"


class DeepSeekStateError(Exception):
    """A user-facing failure tied to one stage of the browser state machine."""

    def __init__(self, stage: str, message: str):
        self.stage = stage
        self.message = message
        self.page = None
        super().__init__(f"[{stage}] {message}")


def _browser_operator_api():
    try:
        from data.plugins.astrbot_plugin_browser_operator.main import (
            get_browser_operation_lock,
            get_browser_page_for_event,
            get_browser_temp_dir_for_event,
        )

        return get_browser_operation_lock, get_browser_page_for_event, get_browser_temp_dir_for_event
    except Exception as exc:
        raise DeepSeekStateError(
            ST_INIT_PAGE,
            f"browser_operator 公共接口不可用：{str(exc)[:500]}",
        ) from exc


async def _get_browser_page(event: AstrMessageEvent):
    """Get a page through browser_operator so its runtime policy is applied."""
    _, get_page, _ = _browser_operator_api()
    try:
        return await get_page(event)
    except PermissionError as exc:
        raise DeepSeekStateError(ST_INIT_PAGE, str(exc)) from exc
    except DeepSeekStateError:
        raise
    except Exception as exc:
        raise DeepSeekStateError(ST_INIT_PAGE, f"获取浏览器页面失败：{str(exc)[:800]}") from exc


def _get_operation_lock():
    get_lock, _, _ = _browser_operator_api()
    return get_lock()


def _get_temp_dir(event: AstrMessageEvent) -> Path:
    _, _, get_temp_dir = _browser_operator_api()
    try:
        return get_temp_dir(event)
    except Exception as exc:
        raise DeepSeekStateError(ST_INIT_PAGE, f"获取浏览器临时目录失败：{str(exc)[:500]}") from exc


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]


def _normalise_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _config_bool(config: Any, key: str, default: bool = True) -> bool:
    try:
        value = config.get(key, default)
    except Exception:
        return default
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


_MODE_DISCOVERY_JS = r"""
() => {
  const visible = (el) => {
    if (!el) return false;
    const style = getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden' &&
      Number(style.opacity || 1) > 0 && rect.width > 0 && rect.height > 0;
  };
  const text = (el) => (el?.innerText || el?.textContent || '').replace(/\s+/g, ' ').trim();
  const meta = (el) => [
    text(el), el.getAttribute('aria-label') || '', el.getAttribute('title') || '',
    el.getAttribute('data-testid') || '', el.getAttribute('data-role') || '',
    el.getAttribute('role') || ''
  ].join(' ').replace(/\s+/g, ' ').trim();
  const targetFor = (el) => el.closest(
    'button,[role="button"],[role="switch"],[role="checkbox"],[role="radio"],label'
  ) || el;
  const selected = (el) => {
    const chain = [el, el.parentElement, el.closest('button,[role]')].filter(Boolean);
    for (const item of chain) {
      for (const attr of ['aria-pressed', 'aria-checked', 'data-state', 'data-selected']) {
        const value = item.getAttribute(attr);
        if (value != null) {
          if (/^(true|on|selected|checked|active|pressed)$/i.test(value)) return true;
          if (/^(false|off|unselected|unchecked|inactive|released)$/i.test(value)) return false;
        }
      }
      if (item instanceof HTMLInputElement && (item.type === 'checkbox' || item.type === 'radio')) {
        return Boolean(item.checked);
      }
      const className = typeof item.className === 'string' ? item.className : '';
      if (/(^|[-_ ])(selected|active|checked|enabled|on|pressed)([-_ ]|$)/i.test(className)) return true;
      if (/(^|[-_ ])(unselected|inactive|unchecked|disabled|off)([-_ ]|$)/i.test(className)) return false;
    }
    return null;
  };
  const find = (patterns) => {
    const candidates = [...document.querySelectorAll(
      'button,[role],[aria-label],[title],[data-testid],[aria-pressed],[aria-checked],[data-state],[data-selected],label'
    )];
    let best = null;
    let bestScore = -1;
    for (const raw of candidates) {
      if (!visible(raw)) continue;
      const target = targetFor(raw);
      if (!visible(target)) continue;
      const haystack = `${meta(raw)} ${meta(target)}`;
      if (!patterns.some((pattern) => new RegExp(pattern, 'i').test(haystack))) continue;
      const targetText = text(target);
      const rawText = text(raw);
      const exactTarget = patterns.some((pattern) => new RegExp(`^(?:${pattern})$`, 'i').test(targetText));
      const exactRaw = patterns.some((pattern) => new RegExp(`^(?:${pattern})$`, 'i').test(rawText));
      const role = target.getAttribute('role') || '';
      const score = (exactTarget ? 100 : 0) + (exactRaw ? 30 : 0)
        + (['radio', 'switch', 'checkbox', 'button'].includes(role) ? 20 : 0)
        + (['aria-pressed', 'aria-checked', 'data-state', 'data-selected']
          .some((attr) => target.hasAttribute(attr)) ? 15 : 0)
        + (targetText.length < 40 ? 5 : 0);
      const candidate = {
        found: true,
        text: targetText,
        aria: target.getAttribute('aria-label') || raw.getAttribute('aria-label') || '',
        role: role || target.tagName.toLowerCase(),
        selected: selected(target),
        disabled: Boolean(target.disabled || target.getAttribute('aria-disabled') === 'true'),
        tag: target.tagName.toLowerCase(),
        className: typeof target.className === 'string' ? target.className.slice(0, 160) : ''
      };
      if (score > bestScore) {
        best = candidate;
        bestScore = score;
      }
    }
    return best || {found: false, selected: null};
  };
  return {
    search: find(['^search$', 'search', '联网搜索', '^搜索$']),
    deepThink: find(['deep[ -]?think', '深度思考', '深思']),
    expert: find(['^expert$', 'expert', '专家模式', '^专家$']),
    quick: find(['^instant$', 'instant', '^quick$', 'quick', '快速模式', '即时模式', '^即时$'])
  };
}
"""


_CLICK_MODE_JS = r"""
(patterns) => {
  const visible = (el) => {
    if (!el) return false;
    const style = getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden' &&
      Number(style.opacity || 1) > 0 && rect.width > 0 && rect.height > 0;
  };
  const text = (el) => (el?.innerText || el?.textContent || '').replace(/\s+/g, ' ').trim();
  const candidates = [...document.querySelectorAll(
    'button,[role="button"],[role="switch"],[role="checkbox"],[role="radio"],[aria-label],[title],[data-testid],[aria-pressed],[aria-checked],[data-state],[data-selected],label'
  )];
  for (const raw of candidates) {
    if (!visible(raw)) continue;
    const target = raw.closest(
      'button,[role="button"],[role="switch"],[role="checkbox"],[role="radio"],label'
    ) || raw;
    if (!visible(target) || target.disabled || target.getAttribute('aria-disabled') === 'true') continue;
    const haystack = [text(raw), text(target), raw.getAttribute('aria-label') || '',
      raw.getAttribute('title') || '', raw.getAttribute('data-testid') || '',
      target.getAttribute('aria-label') || '', target.getAttribute('data-testid') || '']
      .join(' ').replace(/\s+/g, ' ');
    if (!patterns.some((pattern) => new RegExp(pattern, 'i').test(haystack))) continue;
    target.click();
    return {clicked: true, text: text(target), role: target.getAttribute('role') || target.tagName.toLowerCase()};
  }
  return {clicked: false};
}
"""


_MESSAGE_SNAPSHOT_JS = r"""
() => {
  const visible = (el) => {
    if (!el) return false;
    const style = getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden' &&
      Number(style.opacity || 1) > 0 && rect.width > 0 && rect.height > 0;
  };
  const clean = (value) => (value || '').replace(/\u200b/g, '').replace(/\s+/g, ' ').trim();
  const isUserMeta = (el) => {
    let node = el;
    for (let i = 0; node && i < 5; i++, node = node.parentElement) {
      const value = [node.getAttribute('data-role') || '',
        node.getAttribute('data-message-author-role') || '',
        node.getAttribute('data-testid') || '', node.getAttribute('aria-label') || '',
        typeof node.className === 'string' ? node.className : ''].join(' ');
      if (/(^|[-_ ])(user|human)([-_ ]|$)/i.test(value) || /user-message|message--user/i.test(value)) return true;
    }
    return false;
  };
  const isAssistantMeta = (el) => {
    let node = el;
    for (let i = 0; node && i < 5; i++, node = node.parentElement) {
      const value = [node.getAttribute('data-role') || '',
        node.getAttribute('data-message-author-role') || '',
        node.getAttribute('data-testid') || '', node.getAttribute('aria-label') || '',
        typeof node.className === 'string' ? node.className : ''].join(' ');
      if (/(^|[-_ ])(assistant|bot|ai)([-_ ]|$)/i.test(value) || /assistant-message|message--assistant/i.test(value)) return true;
      if (/(^|[-_ ])(user|human)([-_ ]|$)/i.test(value) || /user-message|message--user/i.test(value)) return false;
    }
    return false;
  };
  const messageText = (el) => {
    const own = clean(el.innerText || el.textContent || '');
    const className = typeof el.className === 'string' ? el.className : '';
    // The current UI exposes the complete answer on
    // .ds-assistant-message-main-content.  Do not replace it with the
    // longest visible child paragraph: the composer virtualizes markdown
    // paragraphs and that would return only one fragment.
    if (/(markdown|prose|message-content|assistant-message-main-content)/i.test(className)) {
      return own;
    }
    const nested = [...el.querySelectorAll(
      '[data-testid*="markdown" i],[data-testid*="content" i],[class*="markdown" i],[class*="prose" i],[class*="message-content" i]'
    )].filter(visible).map((item) => clean(item.innerText || item.textContent || ''));
    return nested.sort((a, b) => b.length - a.length)[0] || own;
  };
  const collect = (kind) => {
    const selectors = kind === 'assistant' ? [
      '[data-role="assistant"]','[data-message-author-role="assistant"]','[data-testid*="assistant" i]',
      '[class*="assistant" i]','[class*="message--assistant" i]','[class*="ds-markdown" i]',
      '[class*="markdown" i]','[class*="ds-message" i]','main [role="article"]','main [role="listitem"]'
    ] : [
      '[data-role="user"]','[data-message-author-role="user"]','[data-testid*="user" i]',
      '[class*="message--user" i]','[class*="user-message" i]','main [data-role="human"]'
    ];
    const result = [];
    const seen = new Set();
    for (const selector of selectors) {
      for (const el of document.querySelectorAll(selector)) {
        if (!visible(el) || seen.has(el)) continue;
        if ([...seen].some((parent) => parent.contains(el))) continue;
        if (kind === 'assistant' && isUserMeta(el)) continue;
        if (kind === 'assistant' && /ds-message/i.test(typeof el.className === 'string' ? el.className : '') &&
            !el.querySelector('[class*="assistant-message-main-content" i],[class*="markdown" i],[class*="message-content" i]')) continue;
        if (kind === 'user' && !isUserMeta(el) && !/(user|human)/i.test(selector)) continue;
        const value = messageText(el);
        if (!value && kind !== 'assistant') continue;
        seen.add(el);
        result.push({
          text: value,
          key: el.getAttribute('data-message-id') || el.getAttribute('data-testid') || el.id || `${kind}-${result.length}`,
          className: typeof el.className === 'string' ? el.className.slice(0, 160) : '',
          role: el.getAttribute('role') || ''
        });
      }
    }
    return result;
  };
  return {
    assistants: collect('assistant'),
    users: collect('user'),
    bodyText: clean(document.body?.innerText || '').slice(0, 20000)
  };
}
"""


_GENERATION_STATE_JS = r"""
() => {
  const visible = (el) => {
    if (!el) return false;
    const style = getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden' &&
      Number(style.opacity || 1) > 0 && rect.width > 0 && rect.height > 0;
  };
  const candidates = [...document.querySelectorAll(
    'button,[role="button"],[aria-label],[title],[data-testid],[aria-busy="true"],'
    + '[class*="loading" i],[class*="typing" i],[class*="generating" i],[class*="streaming" i]'
  )];
  const stop = candidates.find((el) => visible(el) && /(stop|cancel|停止生成|停止回答|取消生成)/i.test(
    [el.innerText || '', el.getAttribute('aria-label') || '', el.getAttribute('title') || '',
      el.getAttribute('data-testid') || '', typeof el.className === 'string' ? el.className : ''].join(' ')
  ));
  const busy = candidates.some((el) => visible(el) && (
    el.getAttribute('aria-busy') === 'true' ||
    /(loading|typing|generating|streaming|思考中|生成中)/i.test(
      [el.innerText || '', el.getAttribute('aria-label') || '', el.getAttribute('data-testid') || '',
        typeof el.className === 'string' ? el.className : ''].join(' ')
    )
  ));
  return {stopFound: Boolean(stop), stopVisible: Boolean(stop && visible(stop)), generating: busy};
}
"""


_PAGE_ISSUES_JS = r"""
() => ({
  url: location.href,
  title: document.title || '',
  visibleText: (document.body?.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 3000)
})
"""


async def _page_issues(page) -> dict:
    try:
        data = await page.evaluate(_PAGE_ISSUES_JS)
    except Exception:
        data = {"url": getattr(page, "url", ""), "title": "", "visibleText": ""}
    url = str(data.get("url", ""))
    title = str(data.get("title", ""))
    text = str(data.get("visibleText", ""))
    combined = f"{url} {title} {text}"
    login = bool(
        re.search(r"/(login|sign[ _-]?in|auth)(?:[/?#]|$)", url, re.I)
        or re.search(r"(sign in to continue|please sign in|please log in|\b(?:log|sign)[ _-]?in\b|登录后|请登录|登录/注册)", combined, re.I)
    )
    # The authenticated chat page keeps old conversation titles in the sidebar.
    # A historical title such as "收不到验证码" must not be treated as a live
    # challenge.  Only use explicit challenge text globally; accept the generic
    # Chinese word when it is accompanied by a login/identity-verification cue.
    captcha = bool(re.search(r"(captcha|recaptcha|verify you are human|人机验证)", combined, re.I))
    if not captcha and re.search(r"验证码", text, re.I):
        captcha = bool(
            re.search(r"(请输入|输入|获取|发送|短信|手机|身份|安全|验证登录|验证身份).{0,24}验证码", text, re.I)
            or re.search(r"验证码.{0,24}(输入|登录|验证|提交)", text, re.I)
        )
    risk = bool(re.search(r"(cloudflare|checking your browser|attention required|cf-chl|风控|访问受限)", combined, re.I))
    network = bool(re.search(r"(network error|connection error|failed to fetch|unable to connect|网络错误|网络异常|无法连接)", combined, re.I))
    return {
        "url": url,
        "title": title,
        "visible_text": text,
        "login": login,
        "captcha": captcha,
        "risk": risk,
        "network": network,
    }


async def _assert_page_ready(page, stage: str) -> None:
    issues = await _page_issues(page)
    if issues["login"]:
        raise DeepSeekStateError(stage, "DeepSeek 当前页面要求登录，请先在 browser_operator 使用的持久 profile 中人工登录一次")
    if issues["captcha"]:
        raise DeepSeekStateError(stage, "DeepSeek 当前页面出现验证码/人机验证，需要人工处理")
    if issues["risk"]:
        raise DeepSeekStateError(stage, "DeepSeek 当前页面出现 Cloudflare/风控拦截，需要人工处理")
    if issues["network"]:
        raise DeepSeekStateError(stage, "DeepSeek 当前页面报告网络错误，请检查网络或代理")


async def get_switch_states(page, event: AstrMessageEvent) -> dict:
    """Discover mode controls and their state using accessible attributes first."""
    del event  # The page itself is already obtained through the event-scoped public API.
    return await page.evaluate(_MODE_DISCOVERY_JS)


async def _click_mode(page, patterns: list[str], label: str) -> None:
    result = await page.evaluate(_CLICK_MODE_JS, patterns)
    if not result.get("clicked"):
        raise DeepSeekStateError(ST_ENSURE_SWITCHES, f"找到了 {label} 的需求，但没有可点击的控件")
    await page.wait_for_timeout(700)


async def ensure_switches_on(
    page,
    event: AstrMessageEvent,
    config: Any = None,
    deep_think: bool = False,
) -> list[str]:
    """Select the requested thinking mode and leave Search state untouched."""
    fixed: list[str] = []
    use_expert_for_deepthink = _config_bool(config, "enable_expert", True)
    patterns = {
        "expert": [r"^expert$", r"expert", r"专家模式", r"^专家$"],
        "deepThink": [r"deep[ -]?think", r"深度思考", r"深思"],
        "search": [r"^search$", r"search", r"联网搜索", r"^搜索$"],
        "quick": [r"^instant$", r"instant", r"^quick$", r"quick", r"快速模式", r"即时模式", r"^即时$"],
    }
    labels = {
        "expert": "Expert",
        "deepThink": "DeepThink/深度思考",
        "search": "Search/智能搜索",
        "quick": "Quick/Instant",
    }

    # DeepSeek hydrates the composer controls after the new-chat route is
    # visible.  Give the accessible controls a short retry window.
    states = await get_switch_states(page, event)
    for _ in range(6):
        target_found = (
            (states.get("deepThink") or {}).get("found")
            or (states.get("expert") or {}).get("found")
            if deep_think
            else (states.get("deepThink") or {}).get("found")
            or (states.get("quick") or {}).get("found")
        )
        if target_found:
            break
        await page.wait_for_timeout(1000)
        states = await get_switch_states(page, event)

    async def select_mode(key: str) -> None:
        control = states.get(key) or {}
        if not control.get("found") or control.get("selected") is True:
            return
        if control.get("selected") is None:
            print(f"[DeepSeek] 当前页面存在 {labels[key]}，但没有可读的选中状态，跳过")
            return
        await _click_mode(page, patterns[key], labels[key])
        fixed.append(labels[key])

    # Expert mode is used for the explicit deep-thinking choice when the
    # current UI exposes it.  Search is deliberately not changed here: the
    # website may support Search together with DeepThink in the future.
    if deep_think and use_expert_for_deepthink:
        await select_mode("expert")
    elif not deep_think:
        await select_mode("quick")
    states = await get_switch_states(page, event)

    deep_control = states.get("deepThink") or {}
    if deep_think:
        if not deep_control.get("found"):
            print(f"[DeepSeek] 当前页面没有 {labels['deepThink']} 控件，按兼容模式继续")
        elif deep_control.get("selected") is not True:
            await _click_mode(page, patterns["deepThink"], labels["deepThink"])
            fixed.append(labels["deepThink"])
            states = await get_switch_states(page, event)
            deep_control = states.get("deepThink") or {}
            if deep_control.get("selected") is not True:
                print(f"[DeepSeek] {labels['deepThink']} 点击后状态不可确认，按兼容模式继续")
    elif deep_control.get("found") and deep_control.get("selected") is True:
        await _click_mode(page, patterns["deepThink"], labels["deepThink"])
        fixed.append("关闭" + labels["deepThink"])
        states = await get_switch_states(page, event)

    return fixed


async def _goto_chat(page) -> None:
    if not page.url.startswith(DEEPSEEK_URL):
        await page.goto(DEEPSEEK_URL, timeout=60000, wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)


async def ensure_new_chat(page, event: AstrMessageEvent) -> None:
    """Open a new chat using visible text/role, with navigation as a fallback."""
    await _goto_chat(page)
    await _assert_page_ready(page, ST_ENSURE_CHAT)
    result = await page.evaluate(
        _CLICK_MODE_JS,
        [r"^new\s+chat$", r"new chat", r"新对话", r"新建对话"],
    )
    if result.get("clicked"):
        await page.wait_for_timeout(1800)
        return
    # Root navigation is a safer fallback than trying to clear an unknown chat DOM.
    await page.goto(DEEPSEEK_URL, timeout=60000, wait_until="domcontentloaded")
    await page.wait_for_timeout(2500)
    await _assert_page_ready(page, ST_ENSURE_CHAT)


async def _find_input(page, stage: str):
    selectors = [
        ('textarea', page.locator('textarea')),
        ('role=textbox', page.locator('[role="textbox"]')),
        ('contenteditable', page.locator('[contenteditable="true"]')),
    ]
    errors = []
    for name, locator in selectors:
        try:
            count = await locator.count()
            for index in range(min(count, 12)):
                candidate = locator.nth(index)
                if await candidate.is_visible() and await candidate.is_enabled():
                    return candidate, name
        except Exception as exc:
            errors.append(f"{name}: {str(exc)[:100]}")
    detail = "; ".join(errors)
    raise DeepSeekStateError(stage, f"找不到可用的消息输入框（已尝试 textarea、role=textbox、contenteditable）{detail}")


async def _input_text(locator) -> str:
    try:
        return _normalise_text(await locator.input_value())
    except Exception:
        try:
            return _normalise_text(await locator.evaluate("el => el.innerText || el.textContent || el.value || ''"))
        except Exception:
            return ""


async def _fill_input(locator, query: str) -> None:
    try:
        await locator.fill(query, timeout=10000)
        return
    except Exception:
        await locator.click(timeout=5000)
        await locator.press("ControlOrMeta+A")
        await locator.press("Backspace")
        await locator.press_sequentially(query, delay=0)


async def _snapshot(page) -> dict:
    return await page.evaluate(_MESSAGE_SNAPSHOT_JS)


def _assistant_texts(snapshot: dict) -> list[str]:
    return [_normalise_text(item.get("text")) for item in snapshot.get("assistants", [])]


def _new_assistant_text(snapshot: dict, baseline: dict) -> str:
    current = snapshot.get("assistants", [])
    old = baseline.get("assistants", [])
    old_texts = {_normalise_text(item.get("text")) for item in old if item.get("text")}
    candidates = []
    for index, item in enumerate(current):
        text = _normalise_text(item.get("text"))
        if not text or text in old_texts:
            continue
        class_name = str(item.get("className") or "").lower()
        # Prefer the semantic final-answer container over thought/tool-status
        # markdown fragments.  The current page can expose all of them as
        # assistant-looking nodes, and the last DOM node is not necessarily
        # the final answer.
        priority = 0
        if "assistant-message-main-content" in class_name:
            priority = 3
        elif "message-content" in class_name or "markdown" in class_name:
            priority = 1
        candidates.append((priority, len(text), index, text))
    if not candidates:
        return ""
    return max(candidates, key=lambda item: (item[0], item[1], item[2]))[3]


async def _verify_sent(page, query: str, baseline: dict) -> dict:
    snapshot = await _snapshot(page)
    prefix = _normalise_text(query)[:80]
    user_found = any(prefix and prefix in _normalise_text(item.get("text")) for item in snapshot.get("users", []))
    body_new = bool(prefix and prefix in snapshot.get("bodyText", "") and prefix not in baseline.get("bodyText", ""))
    return {"user_found": user_found, "body_new": body_new, "snapshot": snapshot}


async def _click_send_button(page) -> Optional[str]:
    patterns = re.compile(r"send|发送|提交|ask", re.I)
    locators = [
        ("role=button", page.get_by_role("button", name=patterns)),
        ("aria-label", page.locator('[aria-label*="send" i], [aria-label*="发送"]')),
        ("data-testid", page.locator('[data-testid*="send" i], [data-testid*="submit" i]')),
    ]
    for name, locator in locators:
        try:
            count = await locator.count()
            for index in range(min(count, 8)):
                candidate = locator.nth(index)
                if await candidate.is_visible() and await candidate.is_enabled():
                    await candidate.click(timeout=5000)
                    return name
        except Exception:
            continue
    result = await page.evaluate(
        _CLICK_MODE_JS,
        [r"^send$", r"send message", r"发送", r"提交", r"^ask$"],
    )
    return "visible-text/role fallback" if result.get("clicked") else None


async def send_and_verify(page, event: AstrMessageEvent, query: str) -> dict:
    """Fill and send a prompt, proving it appeared as this turn's user message."""
    del event
    baseline = await _snapshot(page)
    input_locator, input_method = await _find_input(page, ST_FILL_PROMPT)
    await _fill_input(input_locator, query)
    await page.wait_for_timeout(400)
    try:
        await input_locator.press("Enter")
    except Exception:
        pass
    await page.wait_for_timeout(1600)

    verified = await _verify_sent(page, query, baseline)
    if not verified["user_found"] and not verified["body_new"]:
        send_method = await _click_send_button(page)
        if send_method:
            await page.wait_for_timeout(1800)
            verified = await _verify_sent(page, query, baseline)
    if not verified["user_found"] and not verified["body_new"]:
        raise DeepSeekStateError(
            ST_VERIFY_SENT,
            f"消息发送验证失败：已使用 {input_method} 输入并尝试 Enter/发送按钮，但聊天记录中没有本次问题",
        )
    verified["baseline"] = baseline
    verified["input_method"] = input_method
    return verified


async def wait_for_response_complete(
    page,
    event: AstrMessageEvent,
    baseline: dict,
    timeout_seconds: int = 180,
) -> str:
    """Wait for a new assistant reply using growth, stability and generation signals."""
    del event
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    last_text = ""
    stable_count = 0
    saw_growth = False
    saw_new_reply = False

    while asyncio.get_running_loop().time() < deadline:
        await page.wait_for_timeout(2000)
        snapshot = await _snapshot(page)
        current_text = _new_assistant_text(snapshot, baseline)
        if current_text:
            saw_new_reply = True
            if len(current_text) > len(last_text):
                saw_growth = True
            if current_text == last_text:
                stable_count += 1
            else:
                stable_count = 0
            last_text = current_text
        else:
            stable_count = 0

        generation = await page.evaluate(_GENERATION_STATE_JS)
        stop_visible = bool(generation.get("stopVisible"))
        generating = bool(generation.get("generating"))
        if saw_new_reply and stable_count >= 3 and not stop_visible and not generating:
            print(
                f"[DeepSeek] 回答完成：长度={len(last_text)}，持续增长={saw_growth}，"
                f"停止按钮已隐藏={not stop_visible}"
            )
            return last_text
        if saw_new_reply and stable_count >= 5 and not generation.get("stopFound"):
            # Some UI versions expose neither a stop button nor a generating marker.
            print(f"[DeepSeek] 回答完成：长度={len(last_text)}，页面未提供生成状态指示器")
            return last_text

    if last_text:
        print(f"[DeepSeek] 等待超时，返回当前新回复片段，长度={len(last_text)}")
        return last_text
    raise DeepSeekStateError(ST_WAIT_DONE, f"{timeout_seconds} 秒内没有检测到本次新的 assistant 回复")


async def extract_and_save_reply(
    page,
    event: AstrMessageEvent,
    baseline: dict,
    reply_hint: str = "",
) -> dict:
    """Extract the last assistant message created after the baseline snapshot."""
    snapshot = await _snapshot(page)
    reply = _new_assistant_text(snapshot, baseline) or _normalise_text(reply_hint)
    if not reply:
        raise DeepSeekStateError(ST_EXTRACT, "找不到本次新产生的 assistant 回复（未使用上一轮旧回复兜底）")

    reply_file = None
    preview = reply
    if len(reply) > 4000:
        temp_dir = _get_temp_dir(event)
        temp_dir.mkdir(parents=True, exist_ok=True)
        reply_file = temp_dir / f"deepseek_reply_{_stamp()}.txt"
        reply_file.write_text(reply, encoding="utf-8")
        preview = reply[:3000] + f"\n\n... (共 {len(reply)} 字，完整回复已保存)"
        print(f"[DeepSeek] 长回复已保存到 {reply_file}")
    return {"reply": preview, "full_length": len(reply), "reply_file": str(reply_file) if reply_file else None}


async def _save_error_diagnostics(
    event: AstrMessageEvent,
    page,
    stage: str,
    error: str,
) -> str:
    """Save screenshot plus URL/title/visible text in the browser_operator temp dir."""
    try:
        temp_dir = _get_temp_dir(event)
        temp_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        return ""

    url = ""
    title = ""
    visible_text = ""
    screenshot_path = None
    if page is not None:
        try:
            url = str(page.url)
        except Exception:
            pass
        try:
            title = str(await page.title())
        except Exception:
            pass
        try:
            visible_text = (await page.inner_text("body"))[:3000]
        except Exception:
            pass
        try:
            screenshot_path = temp_dir / f"deepseek_error_{stage.lower()}_{_stamp()}.png"
            await page.screenshot(path=str(screenshot_path), full_page=True)
        except Exception:
            screenshot_path = None

    report_path = temp_dir / f"deepseek_error_{stage.lower()}_{_stamp()}.txt"
    report_path.write_text(
        "\n".join(
            [
                f"stage: {stage}",
                f"error: {error[:2000]}",
                f"url: {url}",
                f"title: {title}",
                "visible_text:",
                visible_text,
                f"screenshot: {screenshot_path or ''}",
            ]
        ),
        encoding="utf-8",
    )
    parts = [f"诊断报告：{report_path}"]
    if screenshot_path:
        parts.append(f"截图：{screenshot_path}")
    return "；".join(parts)


async def _prepare_page(event: AstrMessageEvent, new_chat: bool):
    page = await _get_browser_page(event)
    try:
        if not page.url.startswith(DEEPSEEK_URL):
            await _goto_chat(page)
        await _assert_page_ready(page, ST_INIT_PAGE)
        if new_chat:
            await ensure_new_chat(page, event)
        await _assert_page_ready(page, ST_ENSURE_CHAT)
        return page
    except DeepSeekStateError as exc:
        exc.page = page
        raise


async def do_deepseek_search(
    event: AstrMessageEvent,
    query: str,
    timeout_seconds: int = 180,
    mode_config: Any = None,
    deep_think: bool = False,
) -> dict:
    """Run one turn; deep_think controls thinking mode, not Search state."""
    if not _normalise_text(query):
        raise DeepSeekStateError(ST_FILL_PROMPT, "搜索问题不能为空")
    print(f"[DeepSeek] 开始搜索: {_normalise_text(query)[:80]}...")
    lock = _get_operation_lock()
    page = None
    stage = ST_INIT_PAGE
    async with lock:
        try:
            page = await _prepare_page(event, new_chat=True)
            print("[DeepSeek] 页面和新对话就绪")

            stage = ST_ENSURE_SWITCHES
            fixed = await ensure_switches_on(page, event, mode_config, deep_think=deep_think)
            settings_note = "已自动修正：" + ", ".join(fixed) if fixed else ""
            print(f"[DeepSeek] 模式状态正常 {settings_note}")

            stage = ST_SEND_PROMPT
            sent = await send_and_verify(page, event, query)
            print(f"[DeepSeek] 问题已发送并验证（输入方式：{sent['input_method']}）")

            stage = ST_WAIT_DONE
            reply_hint = await wait_for_response_complete(page, event, sent["baseline"], timeout_seconds)
            stage = ST_EXTRACT
            result = await extract_and_save_reply(page, event, sent["baseline"], reply_hint)
            screenshot_path = None
            try:
                temp_dir = _get_temp_dir(event)
                temp_dir.mkdir(parents=True, exist_ok=True)
                screenshot_path = temp_dir / f"deepseek_response_{_stamp()}.png"
                await page.screenshot(path=str(screenshot_path), full_page=False)
            except Exception:
                pass
            return {
                "query": query,
                "answer": result["reply"],
                "full_length": result["full_length"],
                "reply_file": result["reply_file"],
                "settings_fixed": settings_note,
                "screenshot_path": str(screenshot_path) if screenshot_path else None,
            }
        except DeepSeekStateError as exc:
            if page is None:
                page = exc.page
            diagnostic = await _save_error_diagnostics(event, page, exc.stage, exc.message)
            if diagnostic:
                exc.message = f"{exc.message}；{diagnostic}"
            raise
        except Exception as exc:
            diagnostic = await _save_error_diagnostics(event, page, stage, str(exc))
            message = str(exc)[:1200]
            if diagnostic:
                message += f"；{diagnostic}"
            raise DeepSeekStateError(stage, message) from exc


async def do_deepseek_multi_turn(
    event: AstrMessageEvent,
    queries: list[str],
    timeout_seconds: int = 180,
    mode_config: Any = None,
    deep_think: bool = False,
) -> dict:
    """Run several turns; deep_think controls thinking mode, not Search state."""
    if not queries:
        raise DeepSeekStateError(ST_FILL_PROMPT, "多轮问题列表不能为空")
    print(f"[DeepSeek] 开始多轮对话，共 {len(queries)} 轮")
    lock = _get_operation_lock()
    page = None
    stage = ST_INIT_PAGE
    async with lock:
        try:
            page = await _prepare_page(event, new_chat=False)
            stage = ST_ENSURE_SWITCHES
            fixed = await ensure_switches_on(page, event, mode_config, deep_think=deep_think)
            settings_note = "已自动修正：" + ", ".join(fixed) if fixed else ""
            replies = []
            for index, query in enumerate(queries, start=1):
                stage = ST_SEND_PROMPT
                print(f"[DeepSeek] 第 {index}/{len(queries)} 轮: {_normalise_text(query)[:80]}...")
                sent = await send_and_verify(page, event, query)
                stage = ST_WAIT_DONE
                reply_hint = await wait_for_response_complete(page, event, sent["baseline"], timeout_seconds)
                stage = ST_EXTRACT
                result = await extract_and_save_reply(page, event, sent["baseline"], reply_hint)
                replies.append({
                    "round": index,
                    "query": query,
                    "reply": result["reply"],
                    "full_length": result["full_length"],
                })
            screenshot_path = None
            try:
                temp_dir = _get_temp_dir(event)
                temp_dir.mkdir(parents=True, exist_ok=True)
                screenshot_path = temp_dir / f"deepseek_multi_{_stamp()}.png"
                await page.screenshot(path=str(screenshot_path), full_page=False)
            except Exception:
                pass
            return {
                "rounds": len(queries),
                "replies": replies,
                "settings_fixed": settings_note,
                "screenshot_path": str(screenshot_path) if screenshot_path else None,
            }
        except DeepSeekStateError as exc:
            if page is None:
                page = exc.page
            diagnostic = await _save_error_diagnostics(event, page, exc.stage, exc.message)
            if diagnostic:
                exc.message = f"{exc.message}；{diagnostic}"
            raise
        except Exception as exc:
            diagnostic = await _save_error_diagnostics(event, page, stage, str(exc))
            message = str(exc)[:1200]
            if diagnostic:
                message += f"；{diagnostic}"
            raise DeepSeekStateError(stage, message) from exc


@register("astrbot_plugin_deepseek_search", "虾仁 & 爱音", "DeepSeek搜索与深度思考插件", "0.5.0")
class DeepSeekSearchPlugin(Star):
    def __init__(self, context: Context, config: Any = None):
        super().__init__(context)
        self.config = config or {}

    @filter.llm_tool(name="deepseek_search")
    async def deepseek_search(
        self,
        event: AstrMessageEvent,
        query: str,
        deep_think: bool = False,
    ):
        """使用浏览器中的 DeepSeek 处理需要外部信息或事实核验的问题。

        适合以下情况：
        - 用户明确要求搜索、查询来源或核实信息
        - 询问最新、今天、近期、实时变化的内容
        - 新闻、价格、版本、活动、政策、状态等可能变化的信息
        - 回答需要引用网页来源或日期

        对于稳定的常识、写作、翻译、代码解释等问题，不需要调用此工具。

        Args:
            query(string): 需要查询或核验的问题。
            deep_think(boolean): 是否启用 DeepThink。只切换思考模式，不修改 Search/联网搜索状态。
        """
        try:
            result = await do_deepseek_search(
                event,
                query,
                mode_config=self.config,
                deep_think=bool(deep_think),
            )
            parts = []
            if result["settings_fixed"]:
                parts.append("[" + result["settings_fixed"] + "]")
            parts.append(result["answer"])
            if result["reply_file"]:
                parts.append(f"[完整回复已保存到 {result['reply_file']}]")
            return "\n\n".join(parts)
        except DeepSeekStateError as exc:
            return f"DeepSeek 搜索失败（阶段 {exc.stage}）：{exc.message}"
        except Exception as exc:
            return f"DeepSeek 搜索失败：{str(exc)[:1200]}"

    @filter.llm_tool(name="deepseek_multi_turn")
    async def deepseek_multi_turn(
        self,
        event: AstrMessageEvent,
        topic: str,
        rounds: int = 3,
        deep_think: bool = False,
    ):
        """围绕一个主题进行多轮 DeepSeek 搜索或分析。

        适合需要先查询、再追问、最后汇总的复杂问题；简单问题请使用
        deepseek_search。联网搜索状态由当前 DeepSeek 页面决定。

        Args:
            topic(string): 要查询或分析的主题。
            rounds(number): 对话轮数，范围为 1 到 5。
            deep_think(boolean): 是否启用 DeepThink。只切换思考模式，不修改 Search/联网搜索状态。
        """
        try:
            rounds = min(max(int(rounds), 1), 5)
            queries = [topic]
            if rounds >= 2:
                queries.append("针对你刚才的回答，能否从不同角度再深入分析一下？有哪些关键点是我可能忽略的？")
            if rounds >= 3:
                queries.append("综合你之前的分析，如果要给出实际可操作的建议，你会怎么建议？有没有什么注意事项或风险？")
            if rounds >= 4:
                queries.append("你觉得这个问题还有哪些值得进一步探讨的方向？有没有什么相关的延伸话题值得了解？")
            if rounds >= 5:
                queries.append("最后，能否用最简洁的方式总结一下这次讨论的核心要点？")

            result = await do_deepseek_multi_turn(
                event,
                queries,
                timeout_seconds=180,
                mode_config=self.config,
                deep_think=bool(deep_think),
            )
            parts = []
            if result["settings_fixed"]:
                parts.append("[" + result["settings_fixed"] + "]")
            for item in result["replies"]:
                parts.append(f"--- 第{item['round']}轮 ---\n{item['reply']}")
            return "\n\n".join(parts)
        except DeepSeekStateError as exc:
            return f"DeepSeek 多轮对话失败（阶段 {exc.stage}）：{exc.message}"
        except Exception as exc:
            return f"DeepSeek 多轮对话失败：{str(exc)[:1200]}"

    async def terminate(self):
        pass
