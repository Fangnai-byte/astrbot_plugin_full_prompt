import re
from sys import maxsize

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.api import logger, AstrBotConfig
from astrbot.api.provider import ProviderRequest
from astrbot.core.message.components import At, AtAll, Plain, Reply, Image, Face, Record

DEFAULT_BOT_NAME = "宁宁"

# At 记号的两种写法：我们重建的 `[At:qq]`，以及框架 message_str 里的 `@昵称(qq)`。
# 比对 prompt 与重建文本前统一剥掉，否则多重 @ 的消息永远对不上（2026-10-20 实测：
# 「成员A @机器人 分组名单；…」框架把 @自己 那段删了，重建版用 [At:qq]，护栏判不中）。
_AT_TOKEN_RE = re.compile(r"\[At:[^\]]*\]|@[^\s@()（）]+(?:\([^)]*\))?")


class FullPromptPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config or {}

    # ---------------- 昵称/唤醒词解析 ----------------
    def _parse_kv_map(self, raw) -> dict:
        """统一转为 {str: str}，兼容三种形态：
        list 每项 "号,昵称"（当前面板形态，逐条添加）；dict {号: 昵称}；旧字符串 "号:昵称,号:昵称"。"""
        if isinstance(raw, list):
            out = {}
            for item in raw:
                item = str(item).strip().replace("，", ",")
                if not item or "," not in item:
                    continue
                key, name = item.split(",", 1)
                if name.strip():
                    out[key.strip()] = name.strip()
            return out
        if isinstance(raw, dict):
            return {str(k): str(v).strip() for k, v in raw.items() if str(v).strip()}
        out = {}
        for item in str(raw).split(","):
            item = item.strip()
            if not item or ":" not in item:
                continue
            key, name = item.split(":", 1)
            if name.strip():
                out[key.strip()] = name.strip()
        return out

    def _parse_bot_name_map(self) -> dict:
        """解析按群昵称映射。支持 list('群号,昵称') / dict / 旧字符串。"""
        return self._parse_kv_map(self.config.get("bot_name_map", "") or "")

    def _parse_bot_self_map(self) -> dict:
        """解析按Bot(self_id)昵称映射。支持 list('QQ号,昵称') / dict / 旧字符串。"""
        return self._parse_kv_map(self.config.get("bot_name_by_self_id", "") or "")

    # ---------------- 读 AstrBot 自己配置的唤醒词 ----------------
    @staticmethod
    def _as_list(raw) -> list:
        """把配置里的唤醒词统一成「非空字符串列表」。
        AstrBot 的 wake_prefix 是 list，provider_settings.wake_prefix 是单个字符串，两种都兼容。"""
        if raw is None:
            return []
        items = list(raw) if isinstance(raw, (list, tuple, set)) else [raw]
        out = []
        for item in items:
            item = str(item).strip()
            if item and item not in out:
                out.append(item)
        return out

    def _get_session_config(self, event: AstrMessageEvent):
        """取这条消息所属的 AstrBot 配置档案：每个 bot / 每个会话可以配自己的一串唤醒词。"""
        get_config = getattr(self.context, "get_config", None)
        if not callable(get_config):
            return {}
        umo = str(
            getattr(event, "unified_msg_origin", "")
            or getattr(event, "session", "")
            or ""
        )
        args_list = [(umo,), ()] if umo else [()]
        for args in args_list:
            try:
                conf = get_config(*args)
            except BaseException as e:
                logger.debug(f"[full_prompt] 读取 AstrBot 配置失败(umo={umo!r}): {e}")
                continue
            if conf:
                return conf
        return {}

    def _get_wake_words(self, event: AstrMessageEvent) -> list:
        """本会话可用的唤醒词（保序去重，前面的优先）。
        = AstrBot 配置档案里的 wake_prefix（框架会把它从 message_str 里剥掉）
        + 该档案的额外唤醒前缀 provider_settings.wake_prefix
        + 插件自己补充的 extra_wake_words。
        AstrBot 里配了几个唤醒词这里就认几个，不用再在本插件里逐个 bot 抄一遍。"""
        words = []
        if bool(self.config.get("auto_wake_word", True)):
            conf = self._get_session_config(event)
            words += self._as_list(conf.get("wake_prefix", []))
            p_settings = conf.get("provider_settings") or {}
            if isinstance(p_settings, dict):
                words += self._as_list(p_settings.get("wake_prefix", ""))
        words += self._as_list(self.config.get("extra_wake_words", []) or [])
        out = []
        for w in words:
            if w not in out:
                out.append(w)
        return out

    @staticmethod
    def _leading_text(event: AstrMessageEvent) -> str:
        """原始消息链最前面那段纯文本（遇到 At/图片等组件就停）。"""
        head = ""
        for c in getattr(event.message_obj, "message", []) or []:
            if isinstance(c, Plain):
                head += str(getattr(c, "text", ""))
                continue
            break
        return head.strip()

    def _typed_wake_word(self, event: AstrMessageEvent, wake_words: list) -> str:
        """用户原文里自己打出来的唤醒词。
        AstrBot 的唤醒检测会把它从 event.message_str 里剥掉，但它还留在原始消息链里；按最长匹配认出来。"""
        head = self._leading_text(event)
        if not head:
            return ""
        for word in sorted(wake_words, key=len, reverse=True):
            if word and head.startswith(word):
                return word
        return ""

    @staticmethod
    def _is_name_like(word: str) -> bool:
        """唤醒词像不像名字：含字母/数字/汉字才算，「/」「~」这类纯符号不当名字用。"""
        return bool(re.search(r"[0-9A-Za-z\u3400-\u9fff]", word or ""))

    @staticmethod
    def _platform_at_name(c) -> str:
        """平台在 At 消息段里带的名字（群名片/昵称）。
        AstrBot 解析消息时会把这个名字一起装进 At 组件，它就是**这个群当下对机器人的称呼**，
        直接拿来用，比插件里写死一个名字贴合得多。取不到时返回空串。"""
        name = str(getattr(c, "name", "") or "").strip()
        qq = str(getattr(c, "qq", "") or "")
        if not name or name == qq:
            return ""
        return name

    def _get_bot_name(
        self,
        event: AstrMessageEvent,
        wake_words: list | None = None,
    ) -> str:
        """@机器人 时代入的兜底名字（唤醒词）——只在平台没给 At 名字时用。
        优先级：按群 bot_name_map > 按Bot bot_name_by_self_id >
        本会话配置档案里第一个像名字的唤醒词 > bot_name > 默认宁宁。"""
        gid = str(event.get_group_id() or "")
        if gid:
            name = self._parse_bot_name_map().get(gid)
            if name:
                return name
        self_id = str(event.get_self_id() or "")
        if self_id:
            name = self._parse_bot_self_map().get(self_id)
            if name:
                return name
        if wake_words is None:
            wake_words = self._get_wake_words(event)
        for word in wake_words:
            if self._is_name_like(word):
                return word
        return str(self.config.get("bot_name", "") or "").strip() or DEFAULT_BOT_NAME

    # ---------------- 消息重建 ----------------
    def _rebuild_views(
        self,
        event: AstrMessageEvent,
        wake_words: list | None = None,
    ) -> tuple[str, str]:
        """重建两个视图：

        - full：给 LLM 看的完整文本——别人的 @ 写成 `@昵称(qq)`，自己的 @ 换成机器人名字
        - skeleton：只由**纯文本**拼成的骨架，不含任何 At 渲染，用来跟框架的 prompt 比对
          （框架会把 @自己 整段删掉、别人的 @ 写成 `@昵称(qq)`；比骨架才不受这些表示差异影响）
        """
        if wake_words is None:
            wake_words = self._get_wake_words(event)
        fallback_name = self._get_bot_name(event, wake_words)
        # 用户原文里已经用唤醒词喊过机器人了，@机器人 就不必再占一个名字位（否则会重复两遍）
        already_named = bool(self._typed_wake_word(event, wake_words))
        keep_marker = bool(self.config.get("keep_at_marker", False))
        self_id = str(event.get_self_id() or "")
        parts: list[str] = []
        plain: list[str] = []
        for c in getattr(event.message_obj, "message", []) or []:
            if isinstance(c, Plain):
                text = str(getattr(c, "text", ""))
                parts.append(text)
                plain.append(text)
            elif isinstance(c, AtAll):
                # AtAll 是 At 的子类，必须排在前面判断，否则永远走不到这个分支
                parts.append("[全体成员]")
            elif isinstance(c, At):
                qq = str(getattr(c, "qq", ""))
                if keep_marker:
                    parts.append(f"[At:{qq}]")
                elif qq == self_id:
                    # 平台给了名字就用平台的（适配当前这个群的叫法），没给才退回配置里的唤醒词
                    if not already_named:
                        parts.append(self._platform_at_name(c) or fallback_name)
                else:
                    # 别人的 @：和框架 message_str 一样写成「@昵称(qq)」，模型才看得懂这是谁；
                    # 拿不到昵称时退回 [At:qq]
                    at_name = self._platform_at_name(c)
                    parts.append(f"@{at_name}({qq})" if at_name else f"[At:{qq}]")
            elif isinstance(c, Reply):
                parts.append("")
            elif isinstance(c, Image):
                parts.append("[图片]")
            elif isinstance(c, Face):
                parts.append(f"[表情:{getattr(c, 'id', '')}]")
            elif isinstance(c, Record):
                parts.append("[语音]")
            else:
                parts.append(f"[{getattr(c, 'type', '未知组件')}]")
        return "".join(parts).strip(), "".join(plain).strip()

    def _rebuild_text(
        self,
        event: AstrMessageEvent,
        wake_words: list | None = None,
    ) -> str:
        """兼容旧调用：只取「给 LLM 看」的那一版。"""
        return self._rebuild_views(event, wake_words)[0]

    @staticmethod
    def _normalize(text: str) -> str:
        return re.sub(r"\s+", "", text or "")

    @staticmethod
    def _strip_at_tokens(text: str) -> str:
        """剥掉 At 记号，便于跨表示比对（`[At:qq]` 与框架的 `@昵称(qq)`）。"""
        return _AT_TOKEN_RE.sub("", text or "")

    # ---------------- 钩子 ----------------
    # 优先级最高：本插件的活儿是「把被剥掉的唤醒词 / @ 补回来」，得让其它插件的
    # llm_request 钩子（记忆召回、短语气词判断等）看到的是补全后的原文。
    @filter.on_llm_request(priority=maxsize)
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        """在请求发出前，把被剥离的唤醒词/At补回 prompt。"""
        try:
            if not req.prompt:
                return
            # 排除群：配置了则不补全
            gid = str(event.get_group_id() or "")
            if gid:
                exclude = [str(x) for x in (self.config.get("exclude_groups", []) or [])]
                if gid in exclude:
                    return
            # 私聊开关
            if event.is_private_chat() and not bool(
                self.config.get("enable_private", True)
            ):
                return
            wake_words = self._get_wake_words(event)
            user_text, skeleton = self._rebuild_views(event, wake_words)
            if not user_text:
                return
            a = self._normalize(user_text)
            b = self._normalize(req.prompt)
            if not a or a == b:
                return
            matched = a.endswith(b) or b in a
            if not matched:
                # 框架会把 @自己 整段删掉、别人的 @ 写成「@昵称(qq)」，跟我们重建的文本表示不同；
                # 插入的机器人名字还会把文本切成几段 ⇒ 用「纯文本骨架」比：两边都只剩用户打的字
                sk = self._normalize(skeleton)
                pb = self._strip_at_tokens(b)
                matched = bool(sk) and bool(pb) and (
                    sk == pb or sk.endswith(pb) or pb in sk
                )
            if matched:
                req.prompt = user_text
                logger.debug(
                    f"[full_prompt] 已补全提示词: {req.prompt!r} "
                    f"(唤醒词={wake_words})",
                )
            else:
                # 提示词不是重建文本的一部分（多轮/工具调用改写过），保持不动，只留痕便于排查
                logger.debug(
                    f"[full_prompt] 跳过补全：prompt={req.prompt!r} 不是重建文本 "
                    f"{user_text!r} 的结尾/片段（骨架={skeleton!r}）",
                )
        except BaseException as e:
            logger.debug(f"[full_prompt] 处理失败: {e}")
