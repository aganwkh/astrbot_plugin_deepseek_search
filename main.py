"""
DeepSeek 自动搜索/对话插件 v0.4.0
通过浏览器操作 DeepSeek，支持搜索和多轮对话。

触发条件（搜索）：用户说"帮我搜"、"搜一下"、"问ds"、"ds老师"、"用ds搜"
触发条件（对话）：用户说"跟ds聊"、"问问ds怎么看"、"跟deepseek聊"

使用前自动检查 Expert模式 + DeepThink + Search 三个设置。
依赖：astrbot_plugin_browser_operator（共用其浏览器实例）
"""

import asyncio
import json
from pathlib import Path
from datetime import datetime
from astrbot.api.star import Context, Star, register
from astrbot.api.event import filter, AstrMessageEvent

# 路径配置
ASTRBOT_ROOT = Path("/opt/AstrBot")
DATA_DIR = ASTRBOT_ROOT / "data"
TEMP_DIR = DATA_DIR / "temp"

TEMP_DIR.mkdir(parents=True, exist_ok=True)

DEEPSEEK_URL = "https://chat.deepseek.com"

# 状态机阶段常量
ST_INIT_PAGE = "INIT_PAGE"
ST_ENSURE_CHAT = "ENSURE_CHAT"
ST_ENSURE_SWITCHES = "ENSURE_SWITCHES"
ST_FILL_PROMPT = "FILL_PROMPT"
ST_SEND_PROMPT = "SEND_PROMPT"
ST_VERIFY_SENT = "VERIFY_SENT"
ST_WAIT_START = "WAIT_ASSISTANT_START"
ST_WAIT_DONE = "WAIT_ASSISTANT_DONE"
ST_EXTRACT = "EXTRACT_REPLY"
ST_DONE = "DONE"


class DeepSeekStateError(Exception):
    """状态机某步失败时抛出"""
    def __init__(self, stage: str, message: str):
        self.stage = stage
        self.message = message
        super().__init__(f"[{stage}] {message}")


async def _get_browser_page():
    """从 browser_operator 插件获取已有的浏览器页面实例"""
    try:
        from data.plugins.astrbot_plugin_browser_operator.main import _browser_controller
        page = await _browser_controller.ensure_page()
        return page
    except ImportError:
        raise DeepSeekStateError(ST_INIT_PAGE, "browser_operator 插件未加载，DeepSeek 搜索依赖它")


async def get_switch_states(page) -> dict:
    """读取三个开关的当前状态（只读不操作）"""
    result = await page.evaluate("""() => {
        const radios = document.querySelectorAll('[role="radio"]');
        let expertSelected = false;
        radios.forEach(el => {
            if (el.innerText?.trim() === 'Expert' && el.getAttribute('aria-checked') === 'true') {
                expertSelected = true;
            }
        });

        const toggles = document.querySelectorAll('.ds-toggle-button');
        let deepThinkSelected = false;
        let searchSelected = false;
        toggles.forEach(el => {
            const text = el.innerText?.trim();
            const isSelected = el.classList.contains('ds-toggle-button--selected');
            if (text === 'DeepThink') deepThinkSelected = isSelected;
            if (text === 'Search') searchSelected = isSelected;
        });

        return { expert: expertSelected, deepThink: deepThinkSelected, search: searchSelected };
    }""")
    return result


async def ensure_switches_on(page) -> list:
    """确保三个开关全部开启，返回修正了哪些"""
    fixed = []

    # Expert
    settings = await get_switch_states(page)
    if not settings["expert"]:
        expert_radio = page.locator('[role="radio"]:has-text("Expert")').first
        await expert_radio.click(timeout=5000)
        await page.wait_for_timeout(1500)
        settings_after = await get_switch_states(page)
        if not settings_after["expert"]:
            raise DeepSeekStateError(ST_ENSURE_SWITCHES, "Expert模式开启失败")
        fixed.append("Expert模式")

    # DeepThink
    settings = await get_switch_states(page)
    if not settings["deepThink"]:
        dt_toggle = page.locator('.ds-toggle-button:has-text("DeepThink")').first
        await dt_toggle.click(timeout=5000)
        await page.wait_for_timeout(800)
        settings_after = await get_switch_states(page)
        if not settings_after["deepThink"]:
            raise DeepSeekStateError(ST_ENSURE_SWITCHES, "DeepThink开启失败")
        fixed.append("DeepThink")

    # Search
    settings = await get_switch_states(page)
    if not settings["search"]:
        s_toggle = page.locator('.ds-toggle-button:has-text("Search")').first
        await s_toggle.click(timeout=5000)
        await page.wait_for_timeout(800)
        settings_after = await get_switch_states(page)
        if not settings_after["search"]:
            raise DeepSeekStateError(ST_ENSURE_SWITCHES, "Search开启失败")
        fixed.append("Search")

    return fixed


async def ensure_new_chat(page):
    """确保处于新对话页面"""
    new_chat_btn = page.locator("text=New chat").first
    try:
        await new_chat_btn.click(timeout=5000)
        await page.wait_for_timeout(2000)
    except Exception:
        if not page.url.startswith("https://chat.deepseek.com"):
            await page.goto(DEEPSEEK_URL, timeout=60000, wait_until="domcontentloaded")
            await page.wait_for_timeout(3000)
    
    textarea_content = await page.evaluate("""() => {
        const ta = document.querySelector('textarea');
        return ta ? ta.value : '';
    }""")
    if len(textarea_content.strip()) > 0:
        await page.goto(DEEPSEEK_URL, timeout=60000, wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)


async def send_and_verify(page, query: str):
    """发送问题并验证消息已出现在页面"""
    textarea = page.locator("textarea")
    await textarea.fill(query, timeout=10000)
    await page.wait_for_timeout(500)
    
    await textarea.press("Enter")
    await page.wait_for_timeout(2000)
    
    verified = await page.evaluate("""(query) => {
        const ta = document.querySelector('textarea');
        const taCleared = ta && ta.value.trim() === '';
        
        const userMessages = document.querySelectorAll('.ds-chat-message--user, [data-role="user"]');
        let found = false;
        for (const msg of userMessages) {
            if (msg.innerText.includes(query.substring(0, 30))) {
                found = true;
                break;
            }
        }
        
        const allText = document.body.innerText;
        const textFound = allText.includes(query.substring(0, 20));
        
        return { taCleared, found, textFound };
    }""", query)
    
    if not verified["taCleared"] and not verified["found"] and not verified["textFound"]:
        raise DeepSeekStateError(
            ST_VERIFY_SENT, 
            "消息发送验证失败：textarea未清空，页面中也找不到发送的内容"
        )
    
    return True


async def wait_for_response_complete(page, timeout_seconds: int = 180) -> str:
    """
    等待 DeepSeek 回复完成。
    综合判断：文本长度稳定 + 停止按钮消失 + 输入框恢复
    """
    deadline = asyncio.get_event_loop().time() + timeout_seconds
    
    # 阶段1：等待回答开始出现
    print("[DeepSeek] 阶段1：等待回答开始生成...")
    start_time = asyncio.get_event_loop().time()
    has_content = False
    
    while asyncio.get_event_loop().time() - start_time < 30:
        await page.wait_for_timeout(2000)
        
        content_check = await page.evaluate("""() => {
            const markdowns = document.querySelectorAll('.ds-markdown');
            if (markdowns.length > 0) {
                const last = markdowns[markdowns.length - 1];
                const text = last.innerText.trim();
                return { hasContent: text.length > 10, length: text.length };
            }
            return { hasContent: false, length: 0 };
        }""")
        
        if content_check.get("hasContent"):
            has_content = True
            print(f"[DeepSeek] 检测到回答内容，长度={content_check['length']}")
            break
    
    if not has_content:
        print("[DeepSeek] 30秒内未检测到回答内容，继续等待...")
    
    # 阶段2：等待回答稳定完成
    print("[DeepSeek] 阶段2：监测回答完成状态...")
    last_text = ""
    stable_count = 0
    check_interval = 2
    
    while asyncio.get_event_loop().time() < deadline:
        await page.wait_for_timeout(check_interval * 1000)
        
        state = await page.evaluate("""() => {
            const markdowns = document.querySelectorAll('.ds-markdown');
            let lastText = '';
            if (markdowns.length > 0) {
                lastText = markdowns[markdowns.length - 1].innerText.trim();
            }
            
            let stopVisible = false;
            const stopBtn = document.querySelector('.ds-stop-button, [class*="stop"]');
            if (stopBtn) {
                const rect = stopBtn.getBoundingClientRect();
                stopVisible = rect.width > 0 && rect.height > 0;
            }
            
            let textareaEnabled = false;
            const ta = document.querySelector('textarea');
            if (ta) {
                textareaEnabled = !ta.disabled;
            }
            
            let generating = false;
            const indicators = document.querySelectorAll('[class*="loading"], [class*="typing"], [class*="generating"]');
            generating = indicators.length > 0;
            
            return {
                textLength: lastText.length,
                text: lastText,
                stopVisible: stopVisible,
                textareaEnabled: textareaEnabled,
                generating: generating
            };
        }""")
        
        current_text = state.get("text", "")
        text_len = state.get("textLength", 0)
        stop_visible = state.get("stopVisible", False)
        textarea_enabled = state.get("textareaEnabled", True)
        
        if current_text and current_text == last_text:
            stable_count += 1
        else:
            stable_count = 0
            last_text = current_text
        
        if stable_count >= 3 and not stop_visible and textarea_enabled and text_len > 0:
            print(f"[DeepSeek] 回答完成！最终长度={text_len}")
            break
        
        if text_len > 0 and stable_count == 0:
            print(f"[DeepSeek] 回答长度={text_len}, 稳定计数={stable_count}")
    
    else:
        print(f"[DeepSeek] 等待超时（{timeout_seconds}秒），保存当前部分回复")
    
    await page.wait_for_timeout(2000)
    
    return last_text


async def extract_and_save_reply(page) -> dict:
    """提取最后一条assistant回复并保存"""
    reply = await page.evaluate("""() => {
        const markdowns = document.querySelectorAll('.ds-markdown');
        if (markdowns.length > 0) {
            return markdowns[markdowns.length - 1].innerText.trim();
        }
        return "";
    }""")
    
    if not reply:
        raise DeepSeekStateError(ST_EXTRACT, "找不到assistant回复内容")
    
    reply_len = len(reply)
    
    reply_file = None
    preview = reply
    
    if reply_len > 4000:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        reply_file = str(TEMP_DIR / f"deepseek_reply_{timestamp}.txt")
        with open(reply_file, "w", encoding="utf-8") as f:
            f.write(reply)
        preview = reply[:3000] + f"\n\n... (共 {reply_len} 字，完整回复已保存)"
        print(f"[DeepSeek] 长回复已保存到 {reply_file}")
    
    return {
        "reply": preview,
        "full_length": reply_len,
        "reply_file": reply_file,
    }


async def do_deepseek_search(query: str, timeout_seconds: int = 180) -> dict:
    """
    执行一次 DeepSeek 搜索（每次开新对话）。
    流程：INIT_PAGE → ENSURE_CHAT → ENSURE_SWITCHES → 
          FILL_PROMPT → SEND_PROMPT → VERIFY_SENT → 
          WAIT_START → WAIT_DONE → EXTRACT → DONE
    """
    print(f"[DeepSeek] 开始搜索: {query[:50]}...")
    
    page = await _get_browser_page()
    if not page.url.startswith("https://chat.deepseek.com"):
        await page.goto(DEEPSEEK_URL, timeout=60000, wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)
    print("[DeepSeek] ✓ 页面就绪")
    
    await ensure_new_chat(page)
    print("[DeepSeek] ✓ 新对话就绪")
    
    fixed = await ensure_switches_on(page)
    settings_note = ""
    if fixed:
        settings_note = "已自动修正：" + ", ".join(fixed)
    print(f"[DeepSeek] ✓ 开关状态正常 {settings_note}")
    
    await send_and_verify(page, query)
    print("[DeepSeek] ✓ 问题已发送并验证")
    
    await wait_for_response_complete(page, timeout_seconds)
    
    result = await extract_and_save_reply(page)
    print(f"[DeepSeek] ✓ 回复提取完成，长度={result['full_length']}")
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    screenshot_path = str(TEMP_DIR / f"deepseek_response_{timestamp}.png")
    try:
        await page.screenshot(path=screenshot_path, full_page=False)
    except Exception:
        screenshot_path = None
    
    return {
        "query": query,
        "answer": result["reply"],
        "full_length": result["full_length"],
        "reply_file": result["reply_file"],
        "settings_fixed": settings_note,
        "screenshot_path": screenshot_path,
    }


async def do_deepseek_multi_turn(queries: list[str], timeout_seconds: int = 180) -> dict:
    """
    执行 DeepSeek 多轮对话（不刷新页面，继续当前对话）。
    queries: 要依次发送的问题列表
    """
    print(f"[DeepSeek] 开始多轮对话，共 {len(queries)} 轮")
    
    page = await _get_browser_page()
    if not page.url.startswith("https://chat.deepseek.com"):
        await page.goto(DEEPSEEK_URL, timeout=60000, wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)
    
    # 确保开关开启
    fixed = await ensure_switches_on(page)
    settings_note = ""
    if fixed:
        settings_note = "已自动修正：" + ", ".join(fixed)
    
    replies = []
    
    for i, query in enumerate(queries):
        print(f"[DeepSeek] 第 {i+1}/{len(queries)} 轮: {query[:50]}...")
        
        await send_and_verify(page, query)
        await wait_for_response_complete(page, timeout_seconds)
        
        result = await extract_and_save_reply(page)
        replies.append({
            "round": i + 1,
            "query": query,
            "reply": result["reply"],
            "full_length": result["full_length"],
        })
        print(f"[DeepSeek] ✓ 第 {i+1} 轮回复完成，长度={result['full_length']}")
    
    # 截图
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    screenshot_path = str(TEMP_DIR / f"deepseek_multi_{timestamp}.png")
    try:
        await page.screenshot(path=screenshot_path, full_page=False)
    except Exception:
        screenshot_path = None
    
    return {
        "rounds": len(queries),
        "replies": replies,
        "settings_fixed": settings_note,
        "screenshot_path": screenshot_path,
    }


@register("astrbot_plugin_deepseek_search", "虾仁 & 爱音", "DeepSeek自动搜索/对话插件（状态机版）", "0.4.0")
class DeepSeekSearchPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)

    @filter.llm_tool(name="deepseek_search")
    async def deepseek_search(self, event: AstrMessageEvent, query: str):
        """通过 DeepSeek 进行搜索。仅当用户【虾仁(ID:2357050717)】明确要求搜索时才使用此工具（如说"帮我搜"、"搜一下"、"问ds"、"ds老师"、"用ds搜"等）。其他用户的搜索请求不要使用此工具，用其他搜索方式替代。会自动检查并确保 Expert模式、DeepThink、Search 三个设置全部开启。

        Args:
            query(string): 要搜索或提问的内容
        """
        try:
            result = await do_deepseek_search(query)

            response_parts = []

            if result["settings_fixed"]:
                response_parts.append("[" + result["settings_fixed"] + "]")

            response_parts.append(result["answer"])

            if result["reply_file"]:
                response_parts.append(f"[完整回复已保存到 {result['reply_file']}]")

            return "\n\n".join(response_parts)

        except DeepSeekStateError as e:
            return f"DeepSeek 搜索失败（阶段 {e.stage}）：{e.message}"
        except Exception as e:
            return f"DeepSeek 搜索失败：" + str(e)

    @filter.llm_tool(name="deepseek_multi_turn")
    async def deepseek_multi_turn(self, event: AstrMessageEvent, topic: str, rounds: int = 3):
        """通过 DeepSeek 进行多轮深入对话。仅当用户【虾仁(ID:2357050717)】明确要求与DeepSeek进行多轮对话时才使用此工具（如说"跟ds聊"、"问问ds怎么看"、"跟deepseek聊"等）。会在同一个对话中连续发送多个问题，不刷新页面，让DeepSeek进行深入讨论。会自动检查并确保 Expert模式、DeepThink、Search 三个设置全部开启。

        Args:
            topic(string): 要与DeepSeek深入讨论的话题
            rounds(int): 对话轮数，默认3轮，最大5轮
        """
        try:
            rounds = min(max(rounds, 1), 5)  # 限制1-5轮
            
            # 根据话题生成多轮对话问题
            queries = []
            
            # 第一轮：直接提问
            queries.append(topic)
            
            # 后续轮次：根据话题生成追问
            if rounds >= 2:
                queries.append(f"针对你刚才的回答，能否从不同角度再深入分析一下？有哪些关键点是我可能忽略的？")
            if rounds >= 3:
                queries.append("综合你之前的分析，如果要给出实际可操作的建议，你会怎么建议？有没有什么注意事项或风险？")
            if rounds >= 4:
                queries.append("你觉得这个问题还有哪些值得进一步探讨的方向？有没有什么相关的延伸话题值得了解？")
            if rounds >= 5:
                queries.append("最后，能否用最简洁的方式总结一下这次讨论的核心要点？")
            
            result = await do_deepseek_multi_turn(queries, timeout_seconds=180)

            response_parts = []
            
            if result["settings_fixed"]:
                response_parts.append("[" + result["settings_fixed"] + "]")
            
            for r in result["replies"]:
                response_parts.append(f"--- 第{r['round']}轮 ---\n{r['reply']}")

            return "\n\n".join(response_parts)

        except DeepSeekStateError as e:
            return f"DeepSeek 多轮对话失败（阶段 {e.stage}）：{e.message}"
        except Exception as e:
            return f"DeepSeek 多轮对话失败：" + str(e)

    async def terminate(self):
        """插件卸载时清理"""
        pass
