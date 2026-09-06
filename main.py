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

    # ---------------- 昵称解析 ----------------
    def _parse_kv_map(self, raw) -> dict:
        """解析 'k:v,k:v' 或 dict 形式的映射，统一转为 {str: str}。"""
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
        """解析按群昵称映射。支持 dict 或 '群号:昵称,群号:昵称' 字符串。"""
        return self._parse_kv_map(self.config.get("bot_name_map", "") or "")

    def _parse_bot_self_map(self) -> dict:
        """解析按Bot(self_id)昵称映射。支持 dict 或 'self_id:昵称,self_id:昵称' 字符串。"""
        return self._parse_kv_map(self.config.get("bot_name_by_self_id", "") or "")

    def _get_bot_name(self, event: AstrMessageEvent) -> str:
        """返回本次消息使用的机器人昵称（唤醒词）。
        优先级：按群 bot_name_map > 按Bot bot_name_by_self_id > 全局 bot_name > 默认宁宁。"""
        default = str(self.config.get("bot_name", "") or "").strip() or "宁宁"
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
        return default

    # ---------------- 消息重建 ----------------
    def _rebuild_text(self, event: AstrMessageEvent) -> str:
        """从原始消息链重建完整用户文本，把 @机器人 换成昵称。"""
        bot_name = self._get_bot_name(event)
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

    # ---------------- 钩子 ----------------
    @filter.on_llm_request()
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
