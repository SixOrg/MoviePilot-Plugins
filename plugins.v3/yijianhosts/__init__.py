# -*- coding: utf-8 -*-
"""
一键优选hosts 插件（MoviePilot V3）

功能：
1. 自动读取 MP 已配置（启用）站点作为目标站点，支持勾选、全选、反选
2. 对目标站点域名进行 DNS 解析得到候选 IP，使用 CloudflareSpeedTest（cfst）测速优选，取最优 IP
3. 支持自定义站点（每行一个「IP 域名」），直接写入 hosts，不参与测速
4. 所有优选结果统一写入系统 hosts 的插件管理区块；停用插件自动恢复原 hosts
5. 支持定时运行与立即运行（站点优选）
"""

from __future__ import annotations

import base64
import ipaddress
import json
import platform
import re
import shutil
import subprocess
import tarfile
import threading
import time
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from apscheduler.triggers.cron import CronTrigger
from fastapi import Request

from app.db.oper.site import SiteOper
from app.sdk import scheduler as scheduler_sdk
from app.sdk.logging import logger
from app.sdk.plugin import _PluginBase

# 插件在系统 hosts 文件中管理区块的起止标记
HOSTS_MARK_START = "# === 一键优选hosts START ==="
HOSTS_MARK_END = "# === 一键优选hosts END ==="
# hosts 备份数据键（保存原始 hosts 字节的 base64）
HOSTS_BACKUP_KEY = "hosts_backup_v1"
# 最近运行结果数据键
LAST_RESULT_KEY = "last_result_v1"
# 最近运行摘要数据键（单行精简版，供前端「最近运行」展示）
LAST_SUMMARY_KEY = "last_summary_v1"
# 最近一次成功优选结果数据键（IP/延迟/下载/时间，供「结果更优覆盖」比较）
LAST_BEST_KEY = "last_best_v1"
# 写入明细数据键（站点名称/域名/IP 结构化记录，供「写入查看」展示）
WRITE_LOG_KEY = "write_log_v1"
# 当前生效明细数据键（容器重启后重建 hosts 管理区块用）
# 与 WRITE_LOG_KEY 分离：「写入 0 条」的保留型运行不得覆盖真正生效的明细
ACTIVE_LOG_KEY = "active_log_v1"
# CloudflareSpeedTest 官方下载地址（releases/latest/download 命名）
CFST_RELEASE_URL = "https://github.com/XIU2/CloudflareSpeedTest/releases/latest/download/"
# cfst 默认测速参数
CFST_DEFAULT_DELAY = 500  # 平均延迟上限（毫秒）
CFST_DEFAULT_DOWNLOAD_NUM = 10  # 下载测速数量
CFST_DEFAULT_DOWNLOAD_LIMIT = 0  # 下载速度下限（MB/s），0 表示不限制
CFST_DEFAULT_RETRY_COUNT = 0  # 测速未达标重试次数
CFST_DEFAULT_RETRY_INTERVAL = 60  # 测速未达标重试间隔（秒）
CFST_DEFAULT_OVERWRITE_MODE = "always"  # hosts 覆盖逻辑：always=有结果即覆盖；better=结果更优覆盖
CFST_RUN_TIMEOUT = 600  # 单次测速最长等待秒数
CFST_DOWNLOAD_RETRY = 3  # cfst 下载失败重试次数（指数退避）
ONCE_DELAY = 3  # 一次性任务延迟秒数
NOTIFY_SITE_SHOW = 8  # 通知中站点名称最多列出的数量，超出以「等 N 条」简写


class YijianHosts(_PluginBase):
    """一键优选hosts 插件。"""

    plugin_name = "一键优选hosts"
    plugin_desc = "CloudflareSpeedTest全局优选后将所需站点写入hosts/支持手动自定义"
    plugin_version = "1.2"
    plugin_icon = "https://raw.githubusercontent.com/SixOrg/MoviePilot-Plugins/main/icons/yijianhosts.png"
    plugin_author = "六个橙子"
    author_url = "https://github.com/SixOrg"
    plugin_config_prefix = "yijianhosts_"
    plugin_order = 23
    auth_level = 1

    _enabled = False
    _notify = True
    _cron = "0 4 * * *"
    _delay_limit = CFST_DEFAULT_DELAY
    _download_num = CFST_DEFAULT_DOWNLOAD_NUM
    _download_limit = CFST_DEFAULT_DOWNLOAD_LIMIT
    _retry_count = CFST_DEFAULT_RETRY_COUNT
    _retry_interval = CFST_DEFAULT_RETRY_INTERVAL
    _overwrite_mode = CFST_DEFAULT_OVERWRITE_MODE
    _mirror = ""
    _config: Dict[str, Any] = {}
    _conflicts: List[str] = []
    _last_result = "暂无运行记录"
    _results: List[str] = []
    _failures: List[str] = []
    _running = False
    _run_thread: Optional[threading.Thread] = None
    _cancel_flag = threading.Event()
    _lock = threading.Lock()
    _global_best: Optional[Dict[str, str]] = None

    # ===================== 生命周期 =====================

    def init_plugin(self, config: dict = None) -> None:
        """根据插件配置初始化运行状态。

        关闭插件 = hosts 不生效：恢复备份的原始 hosts（无备份则删除管理区块）；
        开启插件 = hosts 生效：若管理区块不存在，用上次写入明细重建（保证开启即有优选生效）。
        """
        # 启动时自动修正数据库里残留的旧版图标字段（相对文件名 → 完整 URL）
        try:
            from app.db.oper.plugininstance import PluginInstanceOper
            _NEW_ICON = "https://raw.githubusercontent.com/SixOrg/MoviePilot-Plugins/main/icons/yijianhosts.png"
            for _inst in PluginInstanceOper().list_by_source("YijianHosts"):
                if _inst.plugin_icon != _NEW_ICON:
                    PluginInstanceOper().save(instance_id=_inst.instance_id, plugin_icon=_NEW_ICON)
        except Exception:
            pass

        self.stop_service()
        config = config or {}
        self._config = config
        self._enabled = bool(config.get("enabled"))
        self._notify = bool(config.get("notify", True))
        self._cron = str(config.get("cron") or "")
        # 立即运行临时参数（run_now_*）优先于正式配置
        self._delay_limit = int(config.get("run_now_delay_limit")
                                or config.get("delay_limit") or CFST_DEFAULT_DELAY)
        self._download_num = int(config.get("run_now_download_num")
                                 or config.get("download_num") or CFST_DEFAULT_DOWNLOAD_NUM)
        self._download_limit = float(config.get("run_now_download_limit")
                                     or config.get("download_limit") or CFST_DEFAULT_DOWNLOAD_LIMIT)
        self._retry_count = int(config.get("run_now_retry_count")
                                or config.get("retry_count") or CFST_DEFAULT_RETRY_COUNT)
        self._retry_interval = int(config.get("run_now_retry_interval")
                                   or config.get("retry_interval") or CFST_DEFAULT_RETRY_INTERVAL)
        self._overwrite_mode = str(config.get("run_now_overwrite_mode")
                                   or config.get("overwrite_mode")
                                   or CFST_DEFAULT_OVERWRITE_MODE).strip().lower()
        if self._overwrite_mode not in ("better", "always"):
            self._overwrite_mode = CFST_DEFAULT_OVERWRITE_MODE
        mirror_cfg = config.get("run_now_cfst_mirror")
        if mirror_cfg is None:
            mirror_cfg = config.get("cfst_mirror")
        if mirror_cfg is None:
            # 从未配置过镜像时默认填好推荐镜像（https://gh-proxy.com），
            # 用户显式清空（配置为空字符串）则保持空以使用官方直连
            self._mirror = "https://gh-proxy.com"
        else:
            self._mirror = str(mirror_cfg).strip().rstrip("/")
        self._conflicts = self._check_conflicts(config)
        self._last_result = str(self.get_data(LAST_RESULT_KEY) or "暂无运行记录")
        if not self._enabled:
            # 关闭插件：hosts 不生效（恢复备份原文；无备份时删除管理区块）
            if self.get_data(HOSTS_BACKUP_KEY):
                self._restore_hosts()
            else:
                self._remove_block()
            return
        # 开启插件：hosts 生效（区块不存在时用上次写入明细重建）
        self._ensure_hosts_effective()
        # 自定义站点与目标站点冲突提示
        if self._conflicts and self._notify:
            self._send_notify(
                "一键优选hosts-自定义站点冲突",
                "以下域名同时出现在目标站点与自定义站点中：" + "、".join(self._conflicts),
            )
        # 一次性触发任务
        if config.get("onlyonce"):
            self._add_once("run_all", "一键优选hosts-立即运行")
            config["onlyonce"] = False
            self.update_config(config)

    def get_state(self) -> bool:
        """返回插件是否启用。"""
        return self._enabled

    def get_api(self) -> List[Dict[str, Any]]:
        """注册插件 API：配置读取 / 保存 / 立即运行 / 查看写入（供 Vue 联邦页面调用）。"""
        return [
            {"path": "/get_config", "endpoint": self.get_config_api,
             "methods": ["GET"], "auth": "bear", "summary": "获取配置页数据"},
            {"path": "/save_config", "endpoint": self.save_config_api,
             "methods": ["POST"], "auth": "bear", "summary": "保存配置"},
            {"path": "/run_now", "endpoint": self.run_now_api,
             "methods": ["POST"], "auth": "bear", "summary": "立即运行一次"},
            {"path": "/view_hosts", "endpoint": self.view_hosts_api,
             "methods": ["GET"], "auth": "bear", "summary": "查看当前写入 hosts 的优选区块"},
            {"path": "/clear_hosts", "endpoint": self.clear_hosts_api,
             "methods": ["POST"], "auth": "bear", "summary": "清空已写入 hosts 的优选区块"},
        ]

    @staticmethod
    def get_render_mode() -> Tuple[str, str]:
        """声明 Vue 联邦渲染模式，前端构建产物目录为 dist/assets。"""
        return "vue", "dist/assets"

    def get_service(self) -> Optional[List[Dict[str, Any]]]:
        """注册定时运行服务。"""
        if not self.get_state() or not self._cron:
            return []
        return [
            {
                "id": "yijianhosts_scheduler",
                "name": "一键优选hosts定时运行",
                "trigger": CronTrigger.from_crontab(self._cron),
                "func": self.run_all,
                "kwargs": {"trigger": "定时"},
            }
        ]

    def stop_service(self) -> None:
        """清理挂到宿主调度器的一次性任务。"""
        remove_once = getattr(scheduler_sdk, "remove_plugin_once_job", None)
        if not remove_once:
            return
        try:
            remove_once(self.__class__.__name__, "run_all")
        except Exception as err:
            logger.warning(f"一键优选hosts 清理一次性任务失败：{err}")

    # ===================== 配置页面 =====================

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """返回插件配置页面与默认配置（JSON 兼容引导；完整配置请进联邦详情页）。"""
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {"component": "VAlert", "props": {
                                        "type": "info", "variant": "tonal",
                                        "text": "请点击插件卡片进入配置界面完成设置。全局优选：对全部 Cloudflare IP 段测速取全局最优 IP，写入所选站点至系统 hosts；未勾选站点则不写入。关闭插件后 hosts 不生效（恢复为原始内容），重新开启插件即恢复生效。"}},
                                ],
                            }
                        ],
                    }
                ],
            }
        ], {}

    def get_page(self) -> Optional[List[dict]]:
        """Vue 联邦模式：详情页由前端组件渲染，无需返回 JSON 数据。"""
        return []

    # ===================== 联邦页面 API =====================

    def get_config_api(self) -> Dict[str, Any]:
        """配置页初始化数据：开关 / 频率 / 站点 / 自定义站点 / 高级参数 / 最近结果。"""
        site_list = self._site_list()
        site_items = [{"title": s["name"], "value": s["name"]} for s in site_list]
        config = self._config or {}
        csv = str(config.get("run_now_sites_csv") or config.get("sites_csv") or "")
        selected = [n.strip() for n in csv.split(",") if n.strip()]
        # 未勾选即未选取（不再把空勾选解释为全部站点）
        return {
            "enabled": bool(self._enabled),
            "notify": bool(self._notify),
            "cron": self._cron,
            "custom_sites": str(config.get("run_now_custom_sites")
                               or config.get("custom_sites") or ""),
            "cfst_mirror": self._mirror,
            "delay_limit": self._delay_limit,
            "download_num": self._download_num,
            "download_limit": self._download_limit,
            "retry_count": self._retry_count,
            "retry_interval": self._retry_interval,
            "overwrite_mode": self._overwrite_mode,
            "site_items": site_items,
            "sites": selected,
            "last_result": f"最近运行：{self._last_result}",
            "last_summary": str(self.get_data(LAST_SUMMARY_KEY) or ""),
            "running": bool(self._running),
        }

    async def save_config_api(self, request: Request) -> Dict[str, Any]:
        """保存配置页设置（写 DB 并刷新实例内存）。"""
        try:
            try:
                body = await request.json()
            except Exception:
                body = {}
            if not isinstance(body, dict):
                body = {}
            sites = [str(x) for x in (body.get("sites") or []) if x]
            cfg = self.get_config() or {}
            if not isinstance(cfg, dict):
                cfg = {}
            # 保存正式配置时清除「立即运行」残留的临时参数
            for k in list(cfg.keys()):
                if str(k).startswith("run_now_"):
                    cfg.pop(k, None)
            cfg.update({
                "enabled": bool(body.get("enabled")),
                "notify": bool(body.get("notify", True)),
                "cron": str(body.get("cron") or "").strip(),
                "sites_csv": ",".join(sites),
                "custom_sites": str(body.get("custom_sites") or ""),
                "cfst_mirror": str(body.get("cfst_mirror") or "").strip(),
                "delay_limit": self._to_int(body.get("delay_limit"), CFST_DEFAULT_DELAY),
                "download_num": self._to_int(body.get("download_num"), CFST_DEFAULT_DOWNLOAD_NUM),
                "download_limit": self._to_float(body.get("download_limit"), CFST_DEFAULT_DOWNLOAD_LIMIT),
                "retry_count": self._to_int(body.get("retry_count"), CFST_DEFAULT_RETRY_COUNT),
                "retry_interval": self._to_int(body.get("retry_interval"), CFST_DEFAULT_RETRY_INTERVAL),
                "overwrite_mode": str(body.get("overwrite_mode") or CFST_DEFAULT_OVERWRITE_MODE).strip().lower(),
            })
            mode = cfg.get("overwrite_mode")
            if mode not in ("better", "always"):
                cfg["overwrite_mode"] = CFST_DEFAULT_OVERWRITE_MODE
            self.update_config(cfg)
            # V3 的 update_config 只写 DB 不重载实例内存，必须重新初始化以同步内存
            try:
                self.init_plugin(self.get_config())
            except Exception as err:
                logger.error(f"一键优选hosts 保存后刷新内存失败：{err}")
            # 撤销已取消站点：保存配置后按新站点列表修剪 hosts 管理区块，
            # 避免取消勾选的站点仍保留在 hosts 中继续生效
            self._prune_hosts_block()
            conflicts = self._check_conflicts(cfg)
            if conflicts:
                return {"success": True,
                        "message": "配置已保存，但以下域名与目标站点重复：" + "、".join(conflicts)}
            logger.info(f"一键优选hosts 配置已保存：启用={cfg['enabled']} 频率={cfg['cron']} 站点={sites}")
            # 「保存即运行」：前端「立即运行」开关开启时保存，保存成功后立即触发一次全局优选；
            # 若已有运行正在进行，先终止旧任务再启动新的立即运行
            run_now = bool(body.get("run_now"))
            if run_now:
                if not cfg["enabled"]:
                    return {"success": True, "message": "配置已保存；插件未启用，无法立即运行"}
                run_cfg = self.get_config() or {}
                if not isinstance(run_cfg, dict):
                    run_cfg = {}
                run_cfg["run_now_sites_csv"] = ",".join(sites)
                run_cfg["run_now_custom_sites"] = str(body.get("custom_sites") or "")
                run_cfg["run_now_cfst_mirror"] = str(body.get("cfst_mirror") or "").strip()
                run_cfg["run_now_delay_limit"] = self._to_int(body.get("delay_limit"), self._delay_limit)
                run_cfg["run_now_download_num"] = self._to_int(body.get("download_num"), self._download_num)
                self.update_config(run_cfg)
                self.init_plugin(run_cfg)
                logger.info("一键优选hosts 收到保存即运行指令（如有旧运行将先终止）")
                self._request_new_run()
                return {"success": True, "background": True,
                        "message": "配置已保存，已开始运行（后台执行中，完成后页面自动刷新结果）"}
            return {"success": True, "message": "配置已保存"}
        except Exception as err:
            logger.error(f"一键优选hosts 保存配置失败：{err}")
            return {"success": False, "message": f"保存失败：{err}"}

    async def run_now_api(self, request: Request) -> Dict[str, Any]:
        """立即运行一次：仅插件启用时可用，接收当前表单值写入 run_now_* 临时键（仅本次生效）。

        若已有运行正在进行，先终止旧任务，再启动本次新的立即运行。
        """
        try:
            if not self.get_state():
                return {"success": False, "message": "插件未启用，请先在设置中开启「启用插件」并保存"}
            try:
                body = await request.json()
            except Exception:
                body = {}
            if not isinstance(body, dict):
                body = {}
            cfg = self.get_config() or {}
            if not isinstance(cfg, dict):
                cfg = {}
            sites = [str(x) for x in (body.get("sites") or []) if x]
            cfg["run_now_sites_csv"] = ",".join(sites)
            cfg["run_now_custom_sites"] = str(body.get("custom_sites") or "")
            cfg["run_now_cfst_mirror"] = str(body.get("cfst_mirror") or "").strip()
            cfg["run_now_delay_limit"] = self._to_int(body.get("delay_limit"), self._delay_limit)
            cfg["run_now_download_num"] = self._to_int(body.get("download_num"), self._download_num)
            # 立即刷新实例内存并在后台线程执行（不阻塞 MP Web 服务）
            self.update_config(cfg)
            self.init_plugin(cfg)
            logger.info("一键优选hosts 收到立即运行指令（如有旧运行将先终止）")
            self._request_new_run()
            return {"success": True, "background": True,
                    "message": "已开始运行（后台执行中，完成后页面自动刷新结果）"}
        except Exception as err:
            logger.error(f"一键优选hosts 触发立即运行失败：{err}")
            return {"success": False, "message": f"触发失败：{err}"}

    def _run_all_background(self) -> None:
        """后台线程执行站点优选（不阻塞 MP Web 服务）。"""
        try:
            self.run_all("手动")
        except Exception as err:
            if self._cancel_flag.is_set():
                logger.info("一键优选hosts 运行已取消")
            else:
                logger.error(f"一键优选hosts 后台运行失败：{err}")
        finally:
            self._running = False

    def _request_new_run(self) -> None:
        """终止正在运行的旧任务，并立即启动一次新的运行。"""
        self._cancel_flag.set()
        old = self._run_thread
        if old is not None and old.is_alive():
            old.join(timeout=20)
            if old.is_alive():
                logger.warning("一键优选hosts 旧运行未能在超时内停止，将强制启动新运行")
        self._cancel_flag.clear()
        self._run_thread = threading.Thread(target=self._run_all_background, daemon=True)
        self._run_thread.start()

    async def view_hosts_api(self, request: Request) -> Dict[str, Any]:
        """读取当前写入 hosts 的优选区块与写入明细（站点/域名/IP），直观展示写入情况。"""
        try:
            raw = self._read_hosts_bytes()
            text = raw.decode("utf-8", errors="replace")
            pattern = re.compile(
                re.escape(HOSTS_MARK_START) + r"(.*?)" + re.escape(HOSTS_MARK_END), re.S)
            m = pattern.search(text)
            block = m.group(1).strip("\r\n \t") if m else ""
            # 结构化写入明细（当前生效记录，容器重启后仍可展示并用于重建）
            logs = self._active_logs()
            count = len(logs) or len([ln for ln in block.splitlines() if ln.strip()])
            block_present = bool(block.strip())
            if block_present:
                msg = f"已写入 {count} 条"
            elif count:
                msg = (f"hosts 中暂无插件管理区块（容器重启后常见），"
                       f"已保留 {count} 条生效明细，插件启用状态下会自动重建")
            else:
                msg = "暂无写入记录"
            return {"success": True, "block": block, "logs": logs,
                    "block_present": block_present, "message": msg}
        except Exception as err:
            logger.warning(f"一键优选hosts 读取 hosts 失败：{err}")
            return {"success": False, "message": f"读取 hosts 失败：{err}"}

    async def clear_hosts_api(self, request: Request) -> Dict[str, Any]:
        """清空插件已写入 hosts 的内容：删除管理区块并恢复备份的原始 hosts，同时清空写入明细与优选缓存。"""
        try:
            if self.get_data(HOSTS_BACKUP_KEY):
                self._restore_hosts()
            else:
                # 无备份时直接删除管理区块（保留其余内容）
                self._apply_entries([])
            self.save_data(WRITE_LOG_KEY, json.dumps([], ensure_ascii=False))
            self.save_data(ACTIVE_LOG_KEY, json.dumps([], ensure_ascii=False))
            # 写入已全部清空，优选比较缓存同步失效：否则下次运行会以清空前的最优记录做比较，
            # 误判「本次未优于上次」而跳过写入
            self._clear_best_cache()
            self._site_names = []
            return {"success": True, "message": "已清空写入，hosts 已恢复为原始内容"}
        except Exception as err:
            logger.warning(f"一键优选hosts 清空写入失败：{err}")
            return {"success": False, "message": f"清空写入失败：{err}"}

    @staticmethod
    def _to_int(value: Any, default: int) -> int:
        """将输入转换为整数，非法值回退默认。"""
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _to_float(value: Any, default: float) -> float:
        """将输入转换为浮点数，非法值回退默认。"""
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    # ===================== 优选执行 =====================

    def run_all(self, trigger: str = "手动") -> None:
        """全局优选：对全部 Cloudflare IP 段测速一次，取全局最优 IP 写入全部目标站点。

        立即运行仅插件启用时可用（前端禁用 + 后端校验）；
        定时任务仅在启用时注册，这里双重校验插件启用状态。
        自定义站点为「IP 域名」显式指定，直接写入，不参与测速优选。
        """
        if not self.get_state():
            return
        with self._lock:
            if self._running:
                self._failures = ["已有运行正在进行，本次跳过"]
                self._last_result = "已有运行正在进行，本次跳过"
                self.save_data(LAST_RESULT_KEY, self._last_result)
                return
            if self._cancel_flag.is_set():
                return
            self._running = True
        logger.info(f"一键优选hosts 开始运行（{trigger}）")
        try:
            self._results, self._failures = [], []
            self._write_log: List[Dict[str, str]] = []
            self._site_names: List[str] = []
            self._global_best = None
            entries: List[str] = []
            targets = self._target_sites()
            # 自定义站点：直接写入用户指定 IP（立即运行的临时参数优先）
            config = self._config or {}
            custom_raw = config.get("run_now_custom_sites") or config.get("custom_sites")
            custom_entries = self._parse_custom_sites(custom_raw)
            if targets:
                # 第一步：确保 cfst 就绪（自动下载，失败重试）
                if self._cancel_flag.is_set():
                    return
                cfst_bin = self._ensure_cfst()
                if cfst_bin is None:
                    if self._cancel_flag.is_set():
                        return
                    self._failures.append("cfst 未就绪：自动下载失败，请检查网络或配置下载镜像")
                else:
                    # 第二步：对全部 Cloudflare IP 段测速，取全局最优 IP（未达标按配置重试）
                    best = None
                    for attempt in range(max(1, self._retry_count + 1)):
                        if self._cancel_flag.is_set():
                            return
                        best = self._run_cfst(cfst_bin)
                        if best:
                            break
                        if attempt < self._retry_count:
                            wait = max(1, self._retry_interval)
                            logger.warning(f"一键优选hosts 测速未达标，{wait}s 后重试"
                                           f"（第 {attempt + 1}/{self._retry_count} 次）")
                            for _ in range(int(wait * 2)):
                                if self._cancel_flag.is_set():
                                    return
                                time.sleep(0.5)
                    if not best:
                        self._failures.append("全局测速未测出可用 IP")
                    else:
                        self._global_best = best
                        ip = best["ip"]
                        # hosts 覆盖逻辑：结果更优覆盖（仅对优选目标站点生效，自定义站点直写不受影响）
                        skip_target = False
                        prev = None
                        if self._overwrite_mode == "better":
                            prev = self._last_best()
                            # 当前已无生效写入（hosts 被清空或站点全部撤销）时不做「更优」比较：
                            # 否则会拿清空前的缓存误判「未优于上次」而跳过写入
                            if (prev and self._active_logs()
                                    and not YijianHosts._is_better(best, prev)):
                                skip_target = True
                        # 分区模板：[优选结果] 内列出 IP 与参数（顶格），站点名称另列 [写入站点] 分区
                        self._results.append(f"全局优选 IP：{ip}")
                        params: List[str] = []
                        if best.get("delay"):
                            params.append(f"延迟 {best['delay']} ms")
                        if best.get("download"):
                            params.append(f"下载 {best['download']} MB/s")
                        if params:
                            self._results.append("，".join(params))
                        if skip_target:
                            prev_dl = prev.get("download") if prev else ""
                            cur_dl = best.get("download") or ""
                            self._results.append(
                                f"本次优选未优于上次（上次下载 {prev_dl} MB/s，本次 {cur_dl} MB/s），"
                                "保留此前写入的 hosts 记录")
                            # 未生效的优选不更新结果缓存，下次比较仍以当前生效记录为准
                            self._global_best = None
                        else:
                            site_names: List[str] = []
                            for site in targets:
                                domain = site["domain"].strip().lower()
                                if not domain:
                                    continue
                                entries.append(f"{ip}  {domain}")
                                site_names.append(site["name"])
                                self._write_log.append({"site": site["name"], "domain": domain, "ip": ip})
                            self._site_names = site_names
            for ip, domain in custom_entries:
                entries.append(f"{ip}  {domain}")
                self._write_log.append({"site": "自定义", "domain": domain, "ip": ip})
            self._finish_run(entries, trigger)
        finally:
            self._running = False

    def _finish_run(self, entries: List[str], source: str) -> None:
        """统一写入 hosts 并汇总结果、通知。

        本次无任何可写条目（如下载/测速失败）时不触碰 hosts，
        保留此前成功写入的记录，避免失败结果覆盖成功结果。
        """
        if entries:
            applied = self._apply_entries(entries)
        else:
            applied = False
        lines = []
        if self._results:
            lines.append("[优选结果]")
            lines.extend(item for item in self._results)
        # [写入站点] 仅在实际写入成功且本次确有站点时列出（列的是真实写入的站点名称）；
        # 站点较多时简写前 NOTIFY_SITE_SHOW 个并标注「等 N 条」，避免推送界面杂乱
        site_names = getattr(self, "_site_names", [])
        if site_names and applied:
            lines.append("[写入站点]")
            if len(site_names) > NOTIFY_SITE_SHOW:
                lines.append("、".join(site_names[:NOTIFY_SITE_SHOW]) + f" 等 {len(site_names)} 条")
            else:
                lines.append("、".join(site_names))
        # [手动写入] 独立展示自定义站点（用户手动填写的「IP 域名」），每行列出 IP 与域名
        custom_rows = [r for r in (self._write_log or [])
                       if r.get("site") == "自定义" and r.get("ip") and r.get("domain")]
        if custom_rows and applied:
            lines.append("[手动写入]")
            lines.extend(f"{r['ip']}  {r['domain']}" for r in custom_rows)
        if self._failures:
            lines.append("[失败项]")
            lines.extend(item for item in self._failures)
        if not lines:
            lines.append("没有需要优选的站点/域名。")
        lines.append("[运行概述]")
        if applied:
            lines.append(f"已写入 hosts（共 {len(entries)} 条，来源：{source}）")
        elif entries:
            lines.append("写入 hosts 失败！")
        else:
            lines.append("本次无成功条目，保留此前写入的 hosts 记录")
        self._last_result = "\n".join(lines)
        self.save_data(LAST_RESULT_KEY, self._last_result)
        # 保存写入明细（站点/域名/IP），供「写入查看」结构化展示。
        # 仅在本次确实写入 hosts 成功时更新：写入 0 条的保留型运行若覆盖明细，
        # 容器重启后就没有任何数据可用于重建管理区块，页面会显示为空
        write_log = getattr(self, "_write_log", None) or []
        if applied and write_log:
            payload = json.dumps(write_log, ensure_ascii=False)
            self.save_data(WRITE_LOG_KEY, payload)
            self.save_data(ACTIVE_LOG_KEY, payload)
        # 单行摘要：仅保留核心信息（IP/参数/写入条数/来源），供前端「最近运行」精简展示
        summary = self._build_summary(entries, applied, source)
        self.save_data(LAST_SUMMARY_KEY, summary)
        # 优选结果本地缓存：写入成功后保存本次全局优选结果，供「结果更优覆盖」比较
        if applied and self._global_best:
            best = self._global_best
            self.save_data(LAST_BEST_KEY, json.dumps({
                "ip": best.get("ip", ""),
                "delay": best.get("delay") or "",
                "download": best.get("download") or "",
                "ts": int(time.time()),
            }, ensure_ascii=False))
        if self._notify:
            self._send_notify("一键优选hosts-运行完成", self._last_result)
        logger.info(f"一键优选hosts 运行完成（{source}）：写入 {len(entries)} 条，"
                    f"成功 {len(self._write_log or [])} 条，失败 {len(self._failures)} 条")
        # 运行结束后清理「立即运行」临时参数，避免影响后续正式配置
        config = self._config or {}
        if any(str(k).startswith("run_now_") for k in config):
            try:
                cfg = self.get_config() or {}
                if not isinstance(cfg, dict):
                    cfg = {}
                for k in list(cfg.keys()):
                    if str(k).startswith("run_now_"):
                        cfg.pop(k, None)
                self.update_config(cfg)
                self.init_plugin(cfg)
            except Exception as err:
                logger.warning(f"一键优选hosts 清理临时参数失败：{err}")

    # ===================== cfst 集成 =====================

    def _cfst_dir(self) -> Path:
        """返回插件数据目录下的 cfst 工作目录。"""
        work = self.get_data_path() / "cfst"
        work.mkdir(parents=True, exist_ok=True)
        return work

    def _ensure_cfst(self) -> Optional[Path]:
        """确保 cfst 可执行文件存在，不存在则自动下载。"""
        with self._lock:
            exe = "cfst.exe" if platform.system() == "Windows" else "cfst"
            bin_path = self._cfst_dir() / exe
            if bin_path.exists():
                return bin_path
            try:
                return self._download_cfst()
            except Exception as err:
                logger.warning(f"一键优选hosts 下载 cfst 失败：{err}")
                return None

    def _download_cfst(self) -> Path:
        """按当前平台从 GitHub Releases（或镜像）下载并解压 cfst。"""
        work = self._cfst_dir()
        system = platform.system()
        machine = platform.machine().lower()
        if system == "Windows":
            arch = "amd64" if ("amd64" in machine or "x86_64" in machine) else "386"
            asset = f"cfst_windows_{arch}.zip"
        elif system == "Darwin":
            arch = "arm64" if ("aarch64" in machine or "arm64" in machine) else "amd64"
            asset = f"cfst_darwin_{arch}.tar.gz"
        else:
            if "aarch64" in machine or "arm64" in machine:
                arch = "arm64"
            elif "arm" in machine:
                arch = "arm"
            else:
                arch = "amd64"
            asset = f"cfst_linux_{arch}.tar.gz"
        url = f"{self._mirror}/{CFST_RELEASE_URL}{asset}" if self._mirror else f"{CFST_RELEASE_URL}{asset}"
        logger.info(f"一键优选hosts 开始下载 cfst：{url}")
        # 下载（GitHub 直连不稳定，失败按指数退避自动重试）
        last_err: Optional[Exception] = None
        for attempt in range(1, CFST_DOWNLOAD_RETRY + 1):
            if self._cancel_flag.is_set():
                logger.info("一键优选hosts 下载 cfst 已取消")
                raise RuntimeError("运行已取消")
            try:
                request = urllib.request.Request(
                    url, headers={"User-Agent": "Mozilla/5.0 MoviePilot yijianhosts"})
                tmp = work / "cfst_download"
                with urllib.request.urlopen(request, timeout=120) as resp, open(tmp, "wb") as fh:
                    shutil.copyfileobj(resp, fh)
                last_err = None
                break
            except Exception as err:
                last_err = err
                if attempt < CFST_DOWNLOAD_RETRY:
                    wait = 2 * (2 ** (attempt - 1))
                    logger.warning(f"一键优选hosts 下载 cfst 第 {attempt} 次失败：{err}，{wait}s 后重试")
                    for _ in range(int(wait * 2)):
                        if self._cancel_flag.is_set():
                            logger.info("一键优选hosts 下载 cfst 已取消")
                            raise RuntimeError("运行已取消")
                        time.sleep(0.5)
        if last_err is not None:
            raise last_err
        # 解压前清空工作目录旧内容（保留下载中的临时文件），避免残留干扰定位
        for f in work.iterdir():
            if f.name != "cfst_download":
                if f.is_dir():
                    shutil.rmtree(f)
                else:
                    f.unlink()
        # 解压
        if asset.endswith(".zip"):
            with zipfile.ZipFile(tmp) as zf:
                zf.extractall(work)
        else:
            try:
                with tarfile.open(tmp, "r:gz") as tf:
                    tf.extractall(work, filter="data")
            except TypeError:
                with tarfile.open(tmp, "r:gz") as tf:
                    tf.extractall(work)
        tmp.unlink(missing_ok=True)
        # 定位可执行文件：兼容新版（cfst/cfst.exe，可能在子目录）与旧版（CloudflareST）
        found = self._find_cfst_bin(work)
        if found is None:
            raise RuntimeError("cfst 解压后未找到可执行文件")
        exe = "cfst.exe" if system == "Windows" else "cfst"
        target = work / exe
        if found != target:
            if target.exists():
                target.unlink()
            shutil.move(str(found), str(target))
        if system != "Windows":
            target.chmod(0o755)
        # 官方 ip.txt（全部 Cloudflare IP 段）复制到工作目录根，供全量段测速使用
        ip_txt = self._find_ip_txt(work)
        if ip_txt is not None and ip_txt.parent != work:
            shutil.copy2(str(ip_txt), str(work / "ip.txt"))
        elif ip_txt is None:
            logger.warning("一键优选hosts 解压包内未找到官方 ip.txt，全量测速将缺少候选")
        return target

    @staticmethod
    def _find_ip_txt(work: Path) -> Optional[Path]:
        """在解压目录中定位官方 ip.txt（全量 CF 段列表）。"""
        for f in work.rglob("ip.txt"):
            return f
        return None

    @staticmethod
    def _find_cfst_bin(work: Path) -> Optional[Path]:
        """在解压目录中递归定位 cfst 可执行文件（兼容子目录结构与新旧命名）。"""
        candidates = ("cfst", "cfst.exe", "cloudflarest", "cloudflarest.exe")
        for f in work.rglob("*"):
            if f.is_file() and f.name.lower() in candidates:
                return f
        return None

    def _run_cfst(self, cfst_bin: Path) -> Optional[Dict[str, str]]:
        """运行 cfst 对全部 Cloudflare IP 段测速，返回最优一行的 IP 及延迟/下载参数；失败返回 None。

        使用 cfst 官方 ip.txt（全部 CF 段，解压时已复制到工作目录），
        对全量段一次测速，候选空间远大于站点解析出的少数 IP，结果更接近真实最优。
        """
        with self._lock:
            work = self._cfst_dir()
            out_file = work / "result.csv"
            if out_file.exists():
                out_file.unlink()
            cmd = [
                str(cfst_bin), "-o", str(out_file),
                "-p", "0", "-tl", str(self._delay_limit), "-dn", str(self._download_num),
            ]
            try:
                proc = subprocess.Popen(cmd, cwd=str(work),
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception as err:
                logger.warning(f"一键优选hosts 测速执行失败：{err}")
                return None
            deadline = time.time() + CFST_RUN_TIMEOUT
            while proc.poll() is None:
                if self._cancel_flag.is_set():
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except Exception:
                        try:
                            proc.kill()
                        except Exception:
                            pass
                    logger.info("一键优选hosts 测速已取消")
                    return None
                if time.time() > deadline:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except Exception:
                        try:
                            proc.kill()
                        except Exception:
                            pass
                    logger.warning("一键优选hosts 测速超时")
                    return None
                time.sleep(0.5)
            return self._parse_result(out_file, self._download_limit)

    @staticmethod
    def _parse_result(out_file: Path, download_limit: float = 0.0) -> Optional[Dict[str, str]]:
        """解析 cfst 输出 result.csv，返回最优一行的 IP 及延迟/下载等参数；失败或下载低于下限返回 None。

        返回形如 {"ip": "1.1.1.1", "loss": "0.00", "delay": "12.34", "download": "88.90"}，
        延迟/下载缺列时对应键不出现；download_limit > 0 时低于该下限视为未达标。
        """
        if not out_file.exists():
            return None
        try:
            text = out_file.read_text(encoding="utf-8-sig", errors="ignore")
        except Exception:
            return None
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if len(lines) < 2:
            return None
        cols = [c.strip() for c in lines[1].split(",")]
        if not cols:
            return None
        ip = cols[0]
        if not YijianHosts._is_valid_ip(ip):
            return None
        result: Dict[str, str] = {"ip": ip}
        if len(cols) > 4 and cols[3]:
            result["loss"] = cols[3]
        if len(cols) > 5 and cols[4]:
            result["delay"] = cols[4]
        if len(cols) > 6 and cols[5]:
            result["download"] = cols[5]
        # 下载速度下限：低于下限视为未达标（无下载数据时不做限制）
        if download_limit > 0 and result.get("download"):
            try:
                if float(result["download"]) < download_limit:
                    return None
            except (TypeError, ValueError):
                return None
        return result

    # ===================== hosts 管理 =====================

    def _hosts_file(self) -> str:
        """返回当前系统的 hosts 文件路径。"""
        return r"C:\Windows\System32\drivers\etc\hosts" if platform.system() == "Windows" else "/etc/hosts"

    def _read_hosts_bytes(self) -> bytes:
        """读取 hosts 文件原始字节。"""
        path = Path(self._hosts_file())
        if not path.exists():
            return b""
        return path.read_bytes()

    def _write_hosts_bytes(self, content: bytes) -> None:
        """写入 hosts 文件原始字节。"""
        Path(self._hosts_file()).write_bytes(content)

    def _backup_hosts(self) -> None:
        """首次写入前备份原始 hosts（幂等，只备份一次）。"""
        if self.get_data(HOSTS_BACKUP_KEY):
            return
        raw = self._read_hosts_bytes()
        self.save_data(HOSTS_BACKUP_KEY, base64.b64encode(raw).decode("ascii"))

    def _restore_hosts(self) -> None:
        """恢复备份的原始 hosts 并清除备份。"""
        backup = self.get_data(HOSTS_BACKUP_KEY)
        if not backup:
            return
        try:
            raw = base64.b64decode(backup.encode("ascii"))
            self._write_hosts_bytes(raw)
            self.del_data(HOSTS_BACKUP_KEY)
            logger.info("一键优选hosts 已恢复原 hosts")
        except Exception as err:
            logger.warning(f"一键优选hosts 恢复 hosts 失败：{err}")

    def _apply_entries(self, entries: List[str]) -> bool:
        """将优选结果写入 hosts 的插件管理区块（幂等替换）。"""
        with self._lock:
            try:
                self._backup_hosts()
                raw = self._read_hosts_bytes()
                text = raw.decode("utf-8", errors="replace")
                # 移除旧管理区块
                pattern = re.compile(
                    re.escape(HOSTS_MARK_START) + r".*?" + re.escape(HOSTS_MARK_END), re.S)
                text = pattern.sub("", text).rstrip("\r\n")
                if entries:
                    block = (
                        "\n" + HOSTS_MARK_START + "\n"
                        + "\n".join(entries) + "\n"
                        + HOSTS_MARK_END + "\n"
                    )
                    text = text + block
                else:
                    text = text + "\n"
                self._write_hosts_bytes(text.encode("utf-8"))
                return True
            except Exception as err:
                logger.warning(f"一键优选hosts 写入 hosts 失败：{err}")
                self._failures.append(f"写入 hosts 失败：{err}")
                return False

    def _remove_block(self) -> None:
        """删除 hosts 中的插件管理区块（不建立备份，用于停用插件时使 hosts 不生效）。"""
        with self._lock:
            try:
                raw = self._read_hosts_bytes()
                text = raw.decode("utf-8", errors="replace")
                if HOSTS_MARK_START not in text or HOSTS_MARK_END not in text:
                    return
                pattern = re.compile(
                    re.escape(HOSTS_MARK_START) + r".*?" + re.escape(HOSTS_MARK_END), re.S)
                text = pattern.sub("", text).rstrip("\r\n") + "\n"
                self._write_hosts_bytes(text.encode("utf-8"))
                logger.info("一键优选hosts 已删除管理区块（插件关闭，hosts 不生效）")
            except Exception as err:
                logger.warning(f"一键优选hosts 删除管理区块失败：{err}")

    def _active_logs(self) -> List[Dict[str, Any]]:
        """返回当前实际生效的写入明细（优先 active_log_v1，1.0 老数据回退 write_log_v1）。"""
        for key in (ACTIVE_LOG_KEY, WRITE_LOG_KEY):
            raw = self.get_data(key) or ""
            if not raw:
                continue
            try:
                logs = json.loads(raw) if isinstance(raw, str) else raw
            except Exception:
                continue
            if not isinstance(logs, list):
                continue
            rows = [r for r in logs if isinstance(r, dict) and r.get("ip") and r.get("domain")]
            if rows:
                return rows
        return []

    def _ensure_hosts_effective(self) -> None:
        """插件开启时确保 hosts 生效：管理区块不存在时用当前生效明细重建，使开启即生效。"""
        try:
            raw = self._read_hosts_bytes()
            text = raw.decode("utf-8", errors="replace")
            if HOSTS_MARK_START in text and HOSTS_MARK_END in text:
                return
            entries = [f"{r['ip']}  {r['domain']}" for r in self._active_logs()]
            if entries:
                self._apply_entries(entries)
                logger.info(f"一键优选hosts 开启后重建 hosts 管理区块（{len(entries)} 条）")
        except Exception as err:
            logger.warning(f"一键优选hosts 开启生效检查失败：{err}")

    def _prune_hosts_block(self) -> None:
        """按当前配置修剪 hosts 管理区块：取消勾选的站点、已删除的自定义行立即撤销写入。

        保存配置时调用：仅让「当前仍被勾选/仍配置」的站点继续生效，
        修剪后无剩余记录时同步清除优选比较缓存，避免下次运行误判「未优于上次」。
        """
        try:
            if not self._enabled:
                return
            logs = self._active_logs()
            if not logs:
                return
            config = self._config or {}
            custom_raw = config.get("run_now_custom_sites")
            if custom_raw is None:
                custom_raw = config.get("custom_sites")
            allowed: set = set()
            for site in self._target_sites():
                domain = (site.get("domain") or "").strip().lower()
                if domain:
                    allowed.add(domain)
            for _ip, domain in self._parse_custom_sites(custom_raw):
                allowed.add(domain)
            kept = [r for r in logs
                    if str(r.get("domain") or "").strip().lower() in allowed]
            if len(kept) == len(logs):
                return
            entries = [f"{r['ip']}  {r['domain']}" for r in kept
                       if r.get("ip") and r.get("domain")]
            self._apply_entries(entries)
            payload = json.dumps(kept, ensure_ascii=False)
            self.save_data(WRITE_LOG_KEY, payload)
            self.save_data(ACTIVE_LOG_KEY, payload)
            removed = len(logs) - len(kept)
            logger.info(f"一键优选hosts 保存配置后已撤销 {removed} 条站点写入"
                        + ("（管理区块已清空）" if not entries else ""))
            if not entries:
                self._clear_best_cache()
        except Exception as err:
            logger.warning(f"一键优选hosts 修剪 hosts 管理区块失败：{err}")

    # ===================== 站点与配置解析 =====================

    def _site_list(self) -> List[Dict[str, str]]:
        """读取 MP 已配置（启用）站点，返回 [{name, domain}]。"""
        try:
            sites = SiteOper().list_active()
        except Exception as err:
            logger.warning(f"一键优选hosts 读取站点列表失败：{err}")
            return []
        result: List[Dict[str, str]] = []
        for site in sites or []:
            name = (getattr(site, "name", "") or "").strip()
            domain = (getattr(site, "domain", "") or "").strip().lower()
            if not name:
                continue
            result.append({"name": name, "domain": domain})
        return result

    def _target_sites(self) -> List[Dict[str, str]]:
        """根据配置（sites_csv，立即运行临时参数优先）返回实际写入的站点；未勾选任何站点则不写入。"""
        config = self._config or {}
        if "run_now_sites_csv" in config and config.get("run_now_sites_csv") is not None:
            csv = str(config.get("run_now_sites_csv") or "")
        else:
            csv = str(config.get("sites_csv") or "")
        selected = [name.strip() for name in csv.split(",") if name.strip()]
        targets: List[Dict[str, str]] = []
        for site in self._site_list():
            if not site["domain"]:
                continue
            if site["name"] in selected:
                targets.append(site)
        return targets

    @staticmethod
    def _parse_custom_sites(raw: str) -> List[Tuple[str, str]]:
        """解析自定义站点：每行一个「IP 域名」，返回 [(ip, domain)]。"""
        entries: List[Tuple[str, str]] = []
        for line in (raw or "").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) != 2:
                continue
            ip, domain = parts[0], parts[1].strip().lower()
            if YijianHosts._is_valid_ip(ip) and YijianHosts._is_valid_domain(domain):
                entries.append((ip, domain))
        return entries

    def _build_summary(self, entries: List[str], applied: bool, source: str) -> str:
        """生成最近运行单行摘要（仅核心信息：优选 IP/参数/写入条数/来源，不罗列站点）。"""
        if applied:
            parts: List[str] = []
            best = self._global_best
            if best:
                seg = f"全局优选 IP：{best.get('ip', '')}"
                params: List[str] = []
                if best.get("delay"):
                    params.append(f"延迟 {best['delay']} ms")
                if best.get("download"):
                    params.append(f"下载 {best['download']} MB/s")
                if params:
                    seg += f"（{'，'.join(params)}）"
                parts.append(seg)
            custom_n = sum(1 for r in (self._write_log or []) if r.get("site") == "自定义")
            parts.append(f"写入 {len(entries)} 条（自定义 {custom_n} 条）")
            parts.append(f"来源：{source}")
            return "｜".join(parts)
        if entries:
            return "写入 hosts 失败！"
        return "本次无成功条目，保留此前写入的 hosts 记录"

    def _last_best(self) -> Optional[Dict[str, str]]:
        """读取最近一次生效的优选结果（供「结果更优覆盖」比较）。"""
        raw = self.get_data(LAST_BEST_KEY) or ""
        try:
            data = json.loads(raw) if isinstance(raw, str) and raw else None
        except Exception:
            return None
        return data if isinstance(data, dict) else None

    def _clear_best_cache(self) -> None:
        """清除最近一次优选结果缓存：写入被清空/全部撤销后调用，避免下次比较误判「未优于上次」。"""
        try:
            self.del_data(LAST_BEST_KEY)
        except Exception:
            self.save_data(LAST_BEST_KEY, "")

    @staticmethod
    def _is_better(new: Dict[str, str], old: Dict[str, str]) -> bool:
        """判定本次优选结果是否更优：下载速度更高视为更优；缺下载数据时视为更优（按有结果即覆盖）。"""
        new_dl = new.get("download")
        old_dl = old.get("download")
        if new_dl is None or old_dl is None:
            return True
        try:
            return float(new_dl) > float(old_dl)
        except (TypeError, ValueError):
            return True

    def _check_conflicts(self, config: dict) -> List[str]:
        """检查自定义站点域名是否与已选目标站点域名重复（未勾选的站点不算冲突）。"""
        if "run_now_sites_csv" in config and config.get("run_now_sites_csv") is not None:
            csv = str(config.get("run_now_sites_csv") or "")
        else:
            csv = str(config.get("sites_csv") or "")
        selected = [name.strip() for name in csv.split(",") if name.strip()]
        site_domains = {site["domain"] for site in self._site_list()
                        if site.get("domain") and site["name"] in selected}
        conflicts = []
        for _, domain in self._parse_custom_sites(config.get("custom_sites")):
            if domain in site_domains:
                conflicts.append(domain)
        return sorted(conflicts)

    # ===================== 工具方法 =====================

    @staticmethod
    def _is_valid_ip(ip: str) -> bool:
        """校验 IPv4 地址。"""
        try:
            return ipaddress.ip_address(ip).version == 4
        except ValueError:
            return False

    @staticmethod
    def _is_valid_domain(domain: str) -> bool:
        """校验域名（允许 host:port 形式）。"""
        return bool(re.fullmatch(
            r"[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+(?::\d{1,5})?", domain))

    def _add_once(self, job_id: str, name: str) -> None:
        """向宿主调度器追加一次性任务。"""
        add_once = getattr(scheduler_sdk, "add_plugin_once_job", None)
        if not add_once:
            logger.warning("一键优选hosts 宿主调度器不支持一次性任务")
            return
        func = getattr(self, job_id, None)
        if func:
            add_once(self.__class__.__name__, job_id, func, name, delay_seconds=ONCE_DELAY)

    def _send_notify(self, title: str, text: str) -> None:
        """发送通知（沿用宿主通知开关与收件人路由）。"""
        try:
            self.post_message(title=title, text=text)
        except Exception as err:
            logger.warning(f"一键优选hosts 发送通知失败：{err}")
