# -*- coding: utf-8 -*-
"""
feishuzhcmd - 飞书中文命令翻译插件（MoviePilot v3.2.x）

解决的问题：
飞书「机器人自定义菜单」的「发送文字消息」动作，若要实现与MP有效交互，只能输入 / 开头的命令，不支持全中文菜单。使用该插件可实现全中文飞书菜单以及全中文交互。

核心机制（渠道挂接）：
插件在运行时找到 MP 的飞书渠道实例，在其入站消息处理入口（_on_message）之前挂一层拦截：文本命中「中文菜单名 -> 命令」映射表且通过管理员白名单校验时，直接以原生方式触发 EventType.CommandExcute 执行命令，并拦截该
消息（不转发给消息链），MP 永远不会把它当作影视标题搜索。未命中一律放行，不影响「订阅/搜索/洗版 + 片名」等原生自然语言交互。

v1.1 新增：出站清洗
MP 的「订阅列表 / 站点列表」等命令回复里自带一段命令行操作提示
（「可输入：搜索 <id...|all>、删除 <id...>、…、退出。」），在飞书手机端
会被原样渲染成带 &lt; &gt; 的脏代码。插件同时挂接飞书渠道的发送方法，
在卡片真正发出之前，用正则把这段「可输入：…退出。」整段从正文里删掉，
让订阅/站点卡片与其他命令一样整洁。按钮、翻页输入（n/p/ID）不受影响。

v1.2 新增：配置页内置教程入口
把《MP 飞书［中文菜单］交互配置教程（第二版）》PDF 随插件仓库一起托管在
docs/feishu-menu-guide.pdf，配置页顶部提供一个按钮，点击即可在浏览器中
打开该教程（主链接走 jsDelivr，并为国内网络保留 GitHub raw 备用地址）。

保留 UserMessage 事件监听作为兜底：在渠道挂接失效的窗口内，能被消息链放行到 UserMessage 的中文文本仍可翻译。

安全：中文路径绕过了渠道层对 / 命令的管理员校验，插件按
FEISHU_ADMINS / FEISHU_OPEN_ID 配置做白名单校验，仅白名单用户可触发命令；
两者都未配置时拒绝执行一切命令。

兼容性与风险：渠道挂接依赖 MP 内部结构（模块管理器与飞书渠道实例），MP 升级或渠道实例重建后挂接可能失效；插件每 60 秒自动校验并补挂，且保留 UserMessage 兜底，最大限度降低失效窗口。

映射策略：配置页「中文菜单映射表」默认列出全部 14 条推荐映射（每行一条「菜单名=命令」），可直接增删改；# 开头为注释行；清空后保存恢复默认内置。
"""
from __future__ import annotations

import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from app.core.config import settings
from app.log import logger
from app.plugins import _PluginBase
from app.runtime.events import Event, eventmanager
from app.schemas.types import EventType, NotificationChannel

# 默认映射：中文菜单文本 -> MP 标准命令（完整命令文本，含斜杠与参数）。
# 与推荐分组菜单方案对齐：订阅与下载 / 站点与智能 / 系统与媒体。
# 菜单名保持简洁中文（与飞书后台已配置的菜单名一致）：渠道挂接会在
# 消息链把中文当影视标题搜索之前拦截，无需在菜单名上加任何前缀。
DEFAULT_MAPPING: Dict[str, str] = {
    # 订阅与下载
    "订阅列表": "/subscribes",
    "正在下载": "/downloading",
    "下载整理": "/transfer",
    # 站点与智能
    "站点列表": "/sites",
    "同步站点": "/cookiecloud",
    "技能管理": "/skills",
    "会话状态": "/session_status",
    "清除会话": "/clear_session",
    "停止推理": "/stop_agent",
    # 系统与媒体
    "当前版本": "/version",
    "清理缓存": "/clear_cache",
    "重启系统": "/restart",
    "系统更新": "/update",
    "同步媒体库": "/mediaserver_sync",
}

# 配置页映射表的默认文本：每行一条「菜单名=命令」，用户可自行增删改。
DEFAULT_MAPPING_TEXT: str = "\n".join(
    f"{name}={cmd}" for name, cmd in DEFAULT_MAPPING.items()
)

# 渠道挂接的轮询补挂间隔（秒）：渠道实例重建或插件先于渠道加载时自动恢复
_PATCH_RETRY_INTERVAL = 60

# v1.1 出站清洗：匹配 MP 命令回复里自带的命令行操作提示段。
# 结构：独立一段，以「可输入：」开头，以「退出。」结尾（中文句号）。
# 中间是各种命令占位符（<id...|all>、<username>、[2fa]、n、p 等），
# 不同命令这段内容不一样，但首尾标记一致，用非贪婪匹配整段删掉。
_HINT_RE = re.compile(r"可输入[：:].*?退出[`'']?\s*[。\.]", re.DOTALL)

# 配置教程 PDF：随插件仓库一起托管在 docs/ 目录，配置页点按钮直接在浏览器打开。
# 主链接走 jsDelivr（国内访问通常比 GitHub 直连快），备用为 GitHub raw 直链。
GUIDE_URL = (
    "https://cdn.jsdelivr.net/gh/SixOrg/MoviePilot-Plugins@main/"
    "plugins.v2/feishuzhcmd/docs/feishu-menu-guide.pdf"
)
GUIDE_URL_BACKUP = (
    "https://raw.githubusercontent.com/SixOrg/MoviePilot-Plugins/main/"
    "plugins.v2/feishuzhcmd/docs/feishu-menu-guide.pdf"
)

# 出站挂接：飞书 client 上名字含这些关键词的方法才视为发送方法
_SEND_METHOD_HINTS = ("send", "reply", "push", "card", "message", "notify")
# 这些前缀的方法一律不动（配置/解析/状态查询等，不是发送）
_SEND_METHOD_EXCLUDE = (
    "get_", "set_", "list_", "has_", "is_",
    "_on_message", "_parse", "_remember", "_sender_ids",
    "_feishuzhcmd",
)


class feishuzhcmd(_PluginBase):
    """飞书中文命令翻译插件。

    插件 ID 规则（v3.2.2）：market 键、插件类名、目录名三者必须字节级一致，
    本插件统一为 feishuzhcmd（全小写），保证安装事务、运行注册表与前端
    查询使用的 ID 完全相同，避免「身份不一致」与配置页 404。
    """

    plugin_name = "飞书中文命令"
    plugin_desc = "实现飞书自定义中文菜单，使用中文代替MP标准命令并执行（复用自带飞书渠道）"
    plugin_label = "飞书,菜单,中文,命令"
    plugin_version = "1.2"
    plugin_author = "六个橙子"
    author_url = "https://github.com/SixOrg"
    plugin_config_prefix = "feishuzhcmd_"
    plugin_order = 20
    auth_level = 1

    _enabled: bool = False
    _mapping_text: str = ""
    _mapping: Dict[str, str] = {}
    _listener_registered: bool = False
    _patch_thread: Optional[threading.Thread] = None
    _patch_thread_alive: bool = False
    # 已挂接的渠道实例与原始 _on_message 引用，用于停止时还原
    _patched: List[Tuple[Any, Any]] = []

    # ---------------- 生命周期 ----------------

    def init_plugin(self, config: Optional[dict] = None) -> None:
        """根据插件配置初始化：注册 UserMessage 兜底监听 + 挂接飞书渠道。"""
        self.stop_service()
        config = config or {}
        self._enabled = bool(config.get("enabled"))
        self._mapping_text = str(config.get("mapping") or "")
        parsed = self._parse_mapping_text(self._mapping_text)
        # 映射迁移：若检测到整表都是「帮我/请问」聊天意图词前缀的映射
        # （旧版本遗留的映射写法），自动切回简洁中文默认映射，保证菜单名
        # 对得上；正常配置不受影响。
        if parsed and all(
            key.startswith("帮我") or key.startswith("请问") for key in parsed
        ):
            logger.info(
                "feishuzhcmd: 检测到「帮我/请问」前缀映射残留，已自动迁移为简洁中文默认映射"
            )
            parsed = {}
        # 映射表被清空时回退默认内置，避免误删导致插件失效
        self._mapping = parsed if parsed else dict(DEFAULT_MAPPING)
        if self._enabled:
            # 兜底：注册绑定方法监听器，分发时直接调用，不依赖实例解析器
            eventmanager.add_event_listener(
                EventType.UserMessage, self._handle_user_message
            )
            self._listener_registered = True
            # 主路径：先立即挂接一次，再启动轮询补挂线程
            self._ensure_feishu_patch()
            self._start_patch_thread()
            logger.info(
                "feishuzhcmd: 已启用 v%s，映射 %d 条，飞书渠道挂接 %d 个实例",
                self.plugin_version,
                len(self._mapping),
                len(self._patched),
            )

    def get_state(self) -> bool:
        """获取插件启用状态。"""
        return self._enabled

    def stop_service(self) -> None:
        """停止插件后台服务：还原渠道挂接并注销事件监听，避免残留。"""
        if self._listener_registered:
            eventmanager.remove_event_listener(
                EventType.UserMessage, self._handle_user_message
            )
            self._listener_registered = False
        self._stop_patch_thread()
        self._restore_feishu_patch()
        self._enabled = False

    # ---------------- 飞书渠道挂接（主路径） ----------------

    def _start_patch_thread(self) -> None:
        """启动轮询补挂线程：渠道实例重建或插件先于渠道加载时自动恢复挂接。"""
        if self._patch_thread and self._patch_thread.is_alive():
            return
        self._patch_thread_alive = True
        self._patch_thread = threading.Thread(
            target=self._patch_loop, daemon=True, name="feishuzhcmd-patch"
        )
        self._patch_thread.start()

    def _stop_patch_thread(self) -> None:
        """停止轮询补挂线程。"""
        self._patch_thread_alive = False
        if self._patch_thread and self._patch_thread.is_alive():
            self._patch_thread.join(timeout=3)
        self._patch_thread = None

    def _patch_loop(self) -> None:
        """轮询补挂线程体：周期校验并补挂飞书渠道实例。"""
        while self._patch_thread_alive:
            try:
                self._ensure_feishu_patch()
            except Exception as error:
                logger.debug("feishuzhcmd: 渠道补挂检查异常：%s", error)
            time.sleep(_PATCH_RETRY_INTERVAL)

    def _find_feishu_clients(self) -> List[Tuple[Any, Dict[str, Any]]]:
        """返回当前运行中的全部飞书渠道实例与其渠道配置。

        白名单所需的管理员 / 默认 Open ID 存在「通知 → 飞书渠道」的
        渠道配置（NotificationConf.config）里，而不是全局 settings，
        因此挂接时一并取回配置供白名单校验使用。
        """
        try:
            from app.runtime.extensions.module.manager import ModuleManager
            from app.schemas.types import ModuleType

            clients: List[Tuple[Any, Dict[str, Any]]] = []
            manager = ModuleManager()
            for module in manager.get_running_type_modules(ModuleType.Notification):
                subtype = None
                try:
                    subtype = module.get_subtype()
                except Exception:
                    subtype = None
                if subtype != NotificationChannel.Feishu:
                    continue
                try:
                    for conf in module.get_configs().values():
                        client = module.get_instance(conf.name)
                        if client is None:
                            continue
                        client_config = getattr(conf, "config", None) or {}
                        clients.append((client, client_config))
                except Exception:
                    continue
            return clients
        except Exception as error:
            logger.debug("feishuzhcmd: 获取飞书渠道实例失败：%s", error)
            return []

    def _ensure_feishu_patch(self) -> None:
        """对尚未挂接的飞书渠道实例挂接拦截层；渠道重建后自动补挂。"""
        for client, config in self._find_feishu_clients():
            if getattr(client, "_feishuzhcmd_patched", False):
                continue
            self._patch_client(client, config)

    def _patch_client(self, client: Any, config: Dict[str, Any]) -> None:
        """在单个飞书渠道实例上挂接入站拦截 + 出站清洗。"""
        # 入站挂接
        try:
            original = client._on_message
        except Exception as error:
            logger.debug("feishuzhcmd: 读取渠道实例入站方法失败：%s", error)
            return
        # 保存渠道配置（含 FEISHU_ADMINS / FEISHU_OPEN_ID），供白名单校验
        client._feishuzhcmd_admin_config = config
        plugin = self

        def _wrapped(event: Any) -> Any:
            """拦截中文菜单消息：命中映射则触发命令并消费；否则走原始逻辑。"""
            if not plugin._enabled:
                return original(event)
            try:
                consumed = plugin._consume_feishu_message(client, event)
            except Exception as error:
                logger.debug("feishuzhcmd: 渠道拦截逻辑异常，放行原始处理：%s", error)
                consumed = False
            if consumed:
                return None
            return original(event)

        client._on_message = _wrapped
        # 出站挂接：清洗发送前的卡片正文
        self._patch_outbound(client)
        client._feishuzhcmd_patched = True
        self._patched.append((client, original))
        logger.info(
            "feishuzhcmd: 已挂接飞书渠道实例 %s（%s）",
            getattr(client, "_name", "?"),
            getattr(client, "_app_id", "?"),
        )

    def _restore_feishu_patch(self) -> None:
        """还原全部已挂接的渠道实例，避免插件停用后残留拦截。"""
        for client, original in self._patched:
            try:
                if getattr(client, "_feishuzhcmd_patched", False):
                    client._on_message = original
                # 还原出站发送方法
                outbound = getattr(client, "_feishuzhcmd_outbound", None)
                if isinstance(outbound, dict):
                    for name, orig in outbound.items():
                        try:
                            setattr(client, name, orig)
                        except Exception:
                            pass
                if hasattr(client, "_feishuzhcmd_outbound"):
                    delattr(client, "_feishuzhcmd_outbound")
                if hasattr(client, "_feishuzhcmd_patched"):
                    delattr(client, "_feishuzhcmd_patched")
                if hasattr(client, "_feishuzhcmd_admin_config"):
                    delattr(client, "_feishuzhcmd_admin_config")
            except Exception as error:
                logger.debug("feishuzhcmd: 还原渠道挂接失败：%s", error)
        self._patched = []

    # ---------------- 出站清洗（v1.1） ----------------

    def _patch_outbound(self, client: Any) -> None:
        """挂接飞书渠道的发送方法，在消息发出前清洗操作提示段。

        反射遍历 client 实例方法，名字含 send/reply/push/card/message/notify
        且不是配置/解析类方法的，全部包一层：调用前递归清洗 args/kwargs 里
        的字符串，命中「可输入：…退出。」就整段删掉。清洗异常一律放行原方法，
        绝不影响正常发送。
        """
        origin_map: Dict[str, Any] = {}
        plugin = self
        # 调试：列出 client 上所有名字含发送关键词的方法，便于定位
        matched_names: List[str] = []
        for attr_name in dir(client):
            low = attr_name.lower()
            if low.startswith(_SEND_METHOD_EXCLUDE):
                continue
            if not any(h in low for h in _SEND_METHOD_HINTS):
                continue
            matched_names.append(attr_name)
            try:
                original = getattr(client, attr_name)
            except Exception as error:
                logger.info(
                    "feishuzhcmd: 出站挂接 getattr %s 失败：%s", attr_name, error
                )
                continue
            if not callable(original):
                logger.info(
                    "feishuzhcmd: 出站挂接 %s 不可调用，跳过", attr_name
                )
                continue
            if getattr(original, "_feishuzhcmd_outbound_patched", False):
                continue

            def _make_wrapper(orig: Any, name: str) -> Any:
                def _wrapped(*args: Any, **kwargs: Any) -> Any:
                    try:
                        args = tuple(plugin._clean_outbound(a) for a in args)
                        kwargs = {k: plugin._clean_outbound(v) for k, v in kwargs.items()}
                    except Exception as error:
                        logger.info(
                            "feishuzhcmd: 出站清洗异常，放行原方法 %s：%s", name, error
                        )
                    return orig(*args, **kwargs)
                _wrapped._feishuzhcmd_outbound_patched = True
                return _wrapped

            try:
                setattr(client, attr_name, _make_wrapper(original, attr_name))
                origin_map[attr_name] = original
            except Exception as error:
                logger.info(
                    "feishuzhcmd: 出站挂接 setattr %s 失败：%s", attr_name, error
                )
        logger.info(
            "feishuzhcmd: 出站挂接扫描完成，匹配方法 %s，实际挂接 %d 个：%s",
            matched_names,
            len(origin_map),
            list(origin_map.keys()),
        )
        client._feishuzhcmd_outbound = origin_map

    @staticmethod
    def _clean_outbound(obj: Any) -> Any:
        """递归清洗出站消息对象：删掉「可输入：…退出。」整段提示。

        支持 str / dict / list / tuple / bytes / 自定义 SDK 对象（遍历 __dict__）。
        list 里清洗后变空字符串的项会被移除（避免留空 div/空行）。
        """
        if isinstance(obj, str):
            new = _HINT_RE.sub("", obj)
            if new != obj:
                logger.info(
                    "feishuzhcmd: 出站清洗命中 str，原片段：%r", obj[:120]
                )
            return new
        if isinstance(obj, bytes):
            try:
                text = obj.decode("utf-8")
            except Exception:
                return obj
            new = _HINT_RE.sub("", text)
            if new != text:
                logger.info(
                    "feishuzhcmd: 出站清洗命中 bytes，原片段：%r", text[:120]
                )
                return new.encode("utf-8")
            return obj
        if isinstance(obj, dict):
            return {k: feishuzhcmd._clean_outbound(v) for k, v in obj.items()}
        if isinstance(obj, list):
            cleaned: List[Any] = []
            for v in obj:
                cv = feishuzhcmd._clean_outbound(v)
                if isinstance(cv, str) and cv.strip() == "":
                    continue
                # 飞书卡片：markdown element 清洗后 content 变空则整个移除，
                # 避免留下空 div/空行
                if (
                    isinstance(cv, dict)
                    and cv.get("tag") == "markdown"
                    and isinstance(cv.get("content"), str)
                    and not cv["content"].strip()
                ):
                    continue
                cleaned.append(cv)
            return cleaned
        if isinstance(obj, tuple):
            return tuple(feishuzhcmd._clean_outbound(v) for v in obj)
        # 未知对象（lark_oapi SDK 等）：不做就地修改，原样返回，避免破坏 SDK 内部状态
        return obj

    # ---------------- 入站拦截 ----------------

    def _consume_feishu_message(self, client: Any, event: Any) -> bool:
        """尝试消费一条飞书消息：文本命中映射且白名单通过时触发命令。

        消费成功返回 True（调用方不再转发消息链，MP 不会把中文当影视标题
        搜索）；未命中或校验失败返回 False（走原生逻辑）。
        """
        if not self._enabled:
            return False
        message = event.get("message") or {}
        if not isinstance(message, dict):
            return False
        if message.get("message_type") != "text":
            return False
        try:
            text, images, audio_refs, files = client._parse_message_content(message)
        except Exception:
            text = None
        if not text or not str(text).strip():
            return False
        text = str(text).strip()
        if text.startswith("/") or text.startswith("CALLBACK:"):
            return False
        sender = event.get("sender") or {}
        try:
            open_id, user_id = client._sender_ids(sender.get("sender_id"))
        except Exception:
            open_id, user_id = None, None
        userid = open_id or user_id
        if not userid:
            return False
        chat_id = message.get("chat_id")
        # 与原始处理一致：记录用户与会话映射，保证命令结果能正确回复
        try:
            client._remember_user_id_type(open_id=open_id, user_id=user_id)
            client._remember_target(userid=userid, chat_id=chat_id)
        except Exception:
            pass
        return self._trigger_command(client, text, userid)

    def _trigger_command(self, client: Any, text: str, userid: str) -> bool:
        """映射命中 + 白名单通过则执行命令，返回是否已消费该消息。"""
        cmd = self._mapping.get(text)
        if not cmd:
            # 未命中映射：打印实际收到的文本，便于确认飞书菜单发出的内容
            logger.info(
                "feishuzhcmd: 飞书文本未命中映射，放行原生逻辑：%s（userid=%s）",
                text,
                userid,
            )
            return False
        if not self._is_allowed_user(userid, client):
            logger.warning(
                "feishuzhcmd: 用户不在白名单，拒绝执行：%s（userid=%s）", text, userid
            )
            return False
        cmd = cmd.strip()
        if not cmd.startswith("/"):
            cmd = f"/{cmd}"
        if not self._command_registered(cmd):
            logger.warning("feishuzhcmd: 映射目标命令不存在，已忽略：%s -> %s", text, cmd)
            return False
        logger.info(
            "feishuzhcmd: 飞书菜单「%s」映射为命令 %s（userid=%s）", text, cmd, userid
        )
        eventmanager.send_event(
            EventType.CommandExcute,
            {
                "cmd": cmd,
                "user": userid,
                "channel": NotificationChannel.Feishu,
                "source": getattr(client, "_name", "feishu"),
            },
        )
        return True

    # ---------------- 事件处理（兜底） ----------------

    def _handle_user_message(self, event: Event) -> None:
        """兜底用户消息事件：渠道挂接失效窗口内，放行的中文文本仍可翻译。"""
        if not self._enabled:
            return
        data = event.event_data
        if not isinstance(data, dict):
            return
        # 仅处理飞书渠道，避免影响 Telegram / 微信等其他渠道
        if data.get("channel") != NotificationChannel.Feishu:
            return
        text = str(data.get("text") or "").strip()
        if not text or text.startswith("/") or text.startswith("CALLBACK:"):
            return
        # 管理员白名单校验（中文路径绕过渠道层对 / 命令的校验，必须自行把关）
        userid = data.get("userid")
        if not self._is_allowed_user(userid):
            return
        cmd = self._mapping.get(text)
        if not cmd:
            return
        cmd = cmd.strip()
        if not cmd.startswith("/"):
            cmd = f"/{cmd}"
        if not self._command_registered(cmd):
            logger.warning(
                "feishuzhcmd: 映射目标命令不存在，已忽略：%s -> %s", text, cmd
            )
            return
        logger.info("feishuzhcmd: 中文指令「%s」映射为命令 %s", text, cmd)
        eventmanager.send_event(
            EventType.CommandExcute,
            {
                "cmd": cmd,
                "user": userid,
                "channel": data.get("channel"),
                "source": data.get("source"),
            },
        )

    # ---------------- 工具方法 ----------------

    def _is_allowed_user(self, userid, client: Any = None) -> bool:
        """校验用户是否为飞书管理员白名单成员。

        优先读取渠道实例携带的渠道配置（MP 后台「通知 → 飞书渠道」表单，
        NotificationConf.config 中的 FEISHU_ADMINS / FEISHU_OPEN_ID），
        渠道实例缺失时回退全局 settings 同名配置。
        FEISHU_ADMINS 逗号分隔 + FEISHU_OPEN_ID 默认收件人；两者都未配置时
        拒绝执行（保守：中文路径绕过了原生管理员检查）。
        """
        if not userid:
            return False
        userid = str(userid)
        admins = ""
        default_open_id = ""
        channel_config = getattr(client, "_feishuzhcmd_admin_config", None)
        if isinstance(channel_config, dict):
            admins = str(channel_config.get("FEISHU_ADMINS") or "")
            default_open_id = str(channel_config.get("FEISHU_OPEN_ID") or "")
        else:
            admins = str(getattr(settings, "FEISHU_ADMINS", "") or "")
            default_open_id = str(getattr(settings, "FEISHU_OPEN_ID", "") or "")
        allowed = {item.strip() for item in admins.split(",") if item.strip()}
        if default_open_id.strip():
            allowed.add(default_open_id.strip())
        if not allowed:
            return False
        return userid in allowed

    @staticmethod
    def _command_registered(cmd: str) -> bool:
        """校验命令是否存在于 MP 命令注册表（未初始化/未注册均视为不存在）。"""
        try:
            from app.application.commands import get_command as _get_command
            registered = bool(_get_command(cmd.split()[0]))
            if not registered:
                logger.warning("feishuzhcmd: 命令未注册：%s", cmd.split()[0])
            return registered
        except Exception as error:
            logger.warning("feishuzhcmd: 命令注册表查询异常：%s", error)
            return False

    @staticmethod
    def _parse_mapping_text(text: str) -> Dict[str, str]:
        """解析映射表文本：每行一条「菜单名=命令」，# 开头为注释行。"""
        mapping: Dict[str, str] = {}
        for line in str(text or "").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if key and value:
                mapping[key] = value
        return mapping

    # ---------------- 插件契约 ----------------

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """返回插件远程命令列表（本插件无远程命令）。"""
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        """返回插件 API 列表（本插件无对外 API）。"""
        return []

    def get_form(self) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """返回插件配置表单与默认配置：启用开关 + 全量映射表（默认列出 14 条）。"""
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VBtn",
                                        "props": {
                                            "text": "打开配置教程 PDF",
                                            "color": "primary",
                                            "variant": "tonal",
                                            "size": "small",
                                            "href": GUIDE_URL,
                                            "target": "_blank",
                                            "rel": "noopener noreferrer",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 9},
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "variant": "tonal",
                                            "text": (
                                                "《MP 飞书［中文菜单］交互配置教程（第二版）》："
                                                "从注册飞书、建企业自建应用、开权限、配事件与回调，"
                                                "到建中文菜单的完整流程。按钮打不开时用备用地址："
                                                + GUIDE_URL_BACKUP
                                            ),
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "enabled",
                                            "label": "启用插件",
                                            "hint": "关闭后中文菜单文本不翻译、直接放行",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextarea",
                                        "props": {
                                            "model": "mapping",
                                            "label": "中文菜单映射表（每行一条：菜单名=命令）",
                                            "rows": 18,
                                            "no-resize": False,
                                            "placeholder": (
                                                "每行一条：菜单名=命令\n例如：正在下载=/downloading"
                                            ),
                                            "hint": (
                                                "默认已列出全部 14 条推荐映射，可自行增删改；"
                                                "# 开头为注释行；清空后保存将恢复默认内置映射"
                                            ),
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                        ],
                    }
                ],
            }
        ], {
            "enabled": self._enabled,
            "mapping": self._mapping_text or DEFAULT_MAPPING_TEXT,
        }

    def get_page(self) -> Optional[List[Dict[str, Any]]]:
        """返回插件详情页面（本插件无详情页）。"""
        return None
