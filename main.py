import re

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.api import logger, AstrBotConfig
from astrbot.api.provider import ProviderRequest
from astrbot.core.message.components import At, AtAll, Plain, Reply, Image, Face, Record


class FullPromptPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config or {}

    def _rebuild_text(self, event: AstrMessageEvent) -> str:
        """从原始消息链重建完整用户文本，把 @机器人 换成昵称。"""
        bot_name = str(self.config.get("bot_name", "") or "").strip() or "宁宁"
        keep_marker = bool(self.config.get("keep_at_marker", False))
        self_id = str(event.get_self_id() or "")
        parts = []
        for c in getattr(event.message_obj, "message", []) or []:
            if isinstance(c, Plain):
                parts.append(str(getattr(c, "text", "")))
            elif isinstance(c, At):
                qq = str(getattr(c, "qq", ""))
                if keep_marker or qq != self_id:
                    parts.append(f"[At:{qq}]")
                else:
                    parts.append(bot_name)
            elif isinstance(c, AtAll):
                parts.append("[全体成员]")
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
        return "".join(parts).strip()

    @staticmethod
    def _normalize(text: str) -> str:
        return re.sub(r"\s+", "", text or "")

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        """在请求发出前，把被剥离的唤醒词/At补回 prompt。"""
        try:
            if not req.prompt:
                return
            user_text = self._rebuild_text(event)
            if not user_text:
                return
            a = self._normalize(user_text)
            b = self._normalize(req.prompt)
            if not a or a == b:
                return
            if a.endswith(b) or b in a:
                req.prompt = user_text
                logger.debug(f"[full_prompt] 已补全提示词: {req.prompt!r}")
        except BaseException as e:
            logger.debug(f"[full_prompt] 处理失败: {e}")
