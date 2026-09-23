# -*- coding: utf-8 -*-
"""
HHCLUB 憨憨保种区管家插件（MoviePilot V2）
v1.4：
1. 移除全部积分/憨豆预估与公式，只保留目标体积模式
2. 优选逻辑：高倍率档优先（0-1人 > 2-3人 > 4-5人）
3. 面板"达标可得"改为"上次憨豆/上次积分"（rescuesettleinfo 实结值）
4. 新增仿QB风格保种管理页：列出在保种子，支持手动删除（同步删下载器任务+文件）
"""

import re
import time
import threading
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from bs4 import BeautifulSoup

from app.core.config import settings
from app.db.site_oper import SiteOper
from app.helper.downloader import DownloaderHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import NotificationType
from fastapi.responses import HTMLResponse

import requests

lock = threading.Lock()


def size_to_gb(text: str) -> Optional[float]:
    m = re.search(r'([\d.]+)\s*(TB|GB|MB|KB)', text or '', re.I)
    if not m:
        return None
    v = float(m.group(1))
    unit = m.group(2).upper()
    if unit == 'TB':
        return v * 1024
    if unit == 'GB':
        return v
    if unit == 'MB':
        return v / 1024
    if unit == 'KB':
        return v / 1048576
    return v


def tier_of(seeders: Optional[int]) -> int:
    n = seeders or 1
    if n <= 1:
        return 0
    if n <= 3:
        return 1
    if n <= 5:
        return 2
    return 99


TIER_NAMES = ("0-1人", "2-3人", "4-5人")
TIER_BADGE = {
    0: '<span style="display:inline-block;padding:1px 8px;border-radius:10px;background:#22c55e;color:#fff;font-size:11px;">0-1人 ×3.0</span>',
    1: '<span style="display:inline-block;padding:1px 8px;border-radius:10px;background:#3b82f6;color:#fff;font-size:11px;">2-3人 ×2.0</span>',
    2: '<span style="display:inline-block;padding:1px 8px;border-radius:10px;background:#ef4444;color:#fff;font-size:11px;">4-5人 ×1.5</span>',
}


class HHClubButler(_PluginBase):
    plugin_name = "憨憨保种区管家"
    plugin_desc = "自动优选添加及换种工具，独立页面[保种管理器]"
    plugin_icon = "https://raw.githubusercontent.com/SixOrg/MoviePilot-Plugins/main/plugins.v2/hhclubbutler/icon.png"
    plugin_version = "2.0"
    plugin_author = "六个橙子"
    author_url = "https://github.com/SixOrg"
    plugin_config_prefix = "hhclubbutler_"
    plugin_order = 20
    auth_level = 1

    _enabled: bool = False
    _onlyonce: bool = False
    _notify: bool = False
    _cron: str = "30 14 * * *"
    _cookie: str = ""
    _site_url: str = ""
    _uid: str = ""
    _mode: str = "incremental"
    _target_volume: float = 0
    _seeder_cond: str = ""
    _exclude_zero: bool = False
    _downloader: str = ""
    _save_path: str = ""
    _tag: str = ""
    _auto_clean_days: float = 0.0
    _scheduler = None

    _running: bool = False
    _last_result: str = "尚未运行"
    _last_overview: dict = None
    _last_overview_ts: float = 0.0
    _overview_hours: float = 6.0
    _overview_thread: Optional[threading.Thread] = None
    _overview_stop: Optional[threading.Event] = None

    def init_plugin(self, config: dict = None):
        self.stop_service()
        config = config or {}
        self._enabled = bool(config.get("enabled"))
        self._onlyonce = bool(config.get("onlyonce"))
        self._notify = bool(config.get("notify"))
        self._cron = config.get("cron") or "30 14 * * *"
        self._cookie = config.get("cookie") or ""
        self._site_url = (config.get("site_url") or "").rstrip("/")
        self._uid = config.get("uid") or ""
        self._mode = config.get("mode") or "incremental"
        if self._mode not in ("incremental", "wash"):
            self._mode = "incremental"
        try:
            self._target_volume = float(config.get("target_volume") or 0)
        except (TypeError, ValueError):
            self._target_volume = 0
        self._seeder_cond = str(config.get("seeder_cond") or "").strip()
        self._exclude_zero = bool(config.get("exclude_zero"))
        self._downloader = config.get("downloader") or ""
        self._save_path = config.get("save_path") or ""
        self._tag = config.get("tag") or ""
        try:
            self._auto_clean_days = float(config.get("auto_clean_days") or 0)
        except (TypeError, ValueError):
            self._auto_clean_days = 0
        self._auto_clean_days = max(0.0, min(365.0, self._auto_clean_days))
        self._overview_hours = 6.0
        self._start_overview_thread()

        logger.info(f"憨憨保种区管家配置生效：启用={self._enabled} 通知={self._notify} "
                    f"模式={self._mode} 目标体积={self._target_volume:g} GB")

        if self._onlyonce:
            self._onlyonce = False
            self.update_config({
                "enabled": self._enabled,
                "onlyonce": False,
                "notify": self._notify,
                "cron": self._cron,
                "mode": self._mode,
                "target_volume": self._target_volume,
                "seeder_cond": self._seeder_cond,
                "exclude_zero": self._exclude_zero,
                "uid": self._uid,
                "cookie": self._cookie,
                "site_url": self._site_url,
                "downloader": self._downloader,
                "save_path": self._save_path,
                "tag": self._tag,
                "auto_clean_days": self._auto_clean_days,
            })
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            self._scheduler.add_job(
                func=self.run,
                trigger="date",
                run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                name="憨憨保种区管家（立即运行）"
            )
            self._scheduler.start()

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return [{
            "cmd": "/hhan_rescue",
            "event": None,
            "desc": "憨憨保种区管家运行",
            "category": "插件命令",
            "data": {}
        }]

    def get_api(self) -> List[Dict[str, Any]]:
        return [{
            "path": "/run", "endpoint": self.api_run, "methods": ["POST"], "auth": "apikey",
            "summary": "立即运行优选",
        }, {
            "path": "/result", "endpoint": self.api_result, "methods": ["GET"], "auth": "apikey",
            "summary": "最近运行结果",
        }, {
            "path": "/refresh_overview", "endpoint": self.api_refresh_overview,
            "methods": ["GET"], "auth": "apikey", "summary": "立即刷新概况",
        }, {
            "path": "/list_rescue_seeds", "endpoint": self.api_list_rescue_seeds,
            "methods": ["GET"], "auth": "apikey", "summary": "列出当前保种区在保种子",
        }, {
            "path": "/delete_rescue_seed", "endpoint": self.api_delete_rescue_seed,
            "methods": ["POST"], "auth": "apikey", "summary": "删除一颗保种种子（含下载器任务+文件）",
        }, {
            "path": "/rescue_panel", "endpoint": self.api_rescue_panel,
            "methods": ["GET"], "auth": "apikey", "summary": "仿QB保种管理页",
        }]

    def get_service(self) -> List[Dict[str, Any]]:
        if not self.get_state():
            return []
        return [{
            "id": "HHClubButler",
            "name": "憨憨保种区管家保种优选服务",
            "trigger": CronTrigger.from_crontab(self._cron),
            "func": self.run,
            "kwargs": {}
        }]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        downloaders = []
        try:
            for config in DownloaderHelper().get_configs().values():
                downloaders.append({"title": config.name, "value": config.name})
        except Exception as e:
            logger.warning(f"获取下载器列表失败：{e}")
        form = [
            {
                'component': 'VForm',
                'content': [
                    {**self._overview_form_cards()},
                    {
                        'component': 'VRow',
                        'content': [
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 4},
                             'content': [{'component': 'VSwitch', 'props': {'model': 'enabled', 'label': '启用插件'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 4},
                             'content': [{'component': 'VSwitch', 'props': {'model': 'onlyonce', 'label': '立即运行一次'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 4},
                             'content': [{'component': 'VSwitch', 'props': {'model': 'notify', 'label': '发送通知'}}]},
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 6},
                             'content': [{'component': 'VCronField', 'props': {
                                 'model': 'cron', 'label': '执行周期',
                                 'placeholder': '30 14 * * *（每天14:30，保种区14:10更新后）'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 6},
                             'content': [{'component': 'VSelect', 'props': {
                                 'model': 'downloader', 'label': '下载器', 'items': downloaders}}]},
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 6},
                             'content': [{'component': 'VTextField', 'props': {
                                 'model': 'save_path', 'label': '保存路径',
                                 'placeholder': '如 /downloads/下载/保种'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 6},
                             'content': [{'component': 'VTextField', 'props': {
                                 'model': 'tag', 'label': '自定义标签',
                                 'placeholder': '如 hhan（推送到下载器后自动打标）'}}]},
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 6},
                             'content': [{'component': 'VSelect', 'props': {
                                 'model': 'mode', 'label': '优选模式',
                                 'items': [
                                     {'title': '增量优选（不删保种&补齐体积）', 'value': 'incremental'},
                                     {'title': '换种优选（删低档位&补高档位）', 'value': 'wash'}
                                 ]}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 6},
                             'content': [{'component': 'VTextField', 'props': {
                                 'model': 'target_volume',
                                 'label': '目标体积（GB，总保种上限）',
                                 'type': 'number',
                                 'hint': '含已做种总体积，0=不限',
                                 'persistent-hint': True}}]},
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 6},
                             'content': [{'component': 'VTextField', 'props': {
                                 'model': 'seeder_cond',
                                 'label': '做种人数',
                                 'hint': '支持单值或区间，例如 1 或 1-5，留空=系统自行优选',
                                 'persistent-hint': True}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 6},
                             'content': [{'component': 'VTextField', 'props': {
                                 'model': 'auto_clean_days',
                                 'label': '自动清理未完成下载（天）',
                                 'hint': '超过N天仍未下载完成自动删除（含未完成文件）；留空或0=不清理',
                                 'persistent-hint': True}}]},
                        ]
                    },
                ]
            }
        ]
        defaults = {
            "enabled": False, "onlyonce": False, "notify": True,
            "cron": "30 14 * * *", "mode": "incremental",
            "target_volume": 0, "seeder_cond": "", "exclude_zero": False,
            "downloader": "", "save_path": "", "tag": "", "auto_clean_days": 0,
        }
        return form, defaults

    def _overview_form_cards(self) -> dict:
        if not self._last_overview or not self._last_overview.get("ok"):
            return {'component': 'VCard', 'props': {'flat': True},
                    'content': [{'component': 'VCardText', 'props': {},
                                 'text': '尚未运行或未获取到数据（运行一次后此处显示最近概况）'}]}
        ov = self._last_overview
        d = ov["dist"]
        c = ov.get("count", 0) or sum(x["count"] for x in d.values()) or 1
        c01 = d["0-1人"]["count"] / c * 100.0
        c23 = d["2-3人"]["count"] / c * 100.0
        c45 = d["4-5人"]["count"] / c * 100.0

        def stat_col(label, num, sub=""):
            return {'component': 'VCol', 'props': {'cols': 6, 'md': 3},
                    'content': [
                        {'component': 'div',
                         'props': {'style': 'text-align:center;font-size:11px;color:rgba(var(--v-theme-on-surface),.6);white-space:nowrap;'},
                         'text': label},
                        {'component': 'div',
                         'props': {'style': 'text-align:center;font-size:17px;font-weight:700;white-space:nowrap;'},
                         'text': f'{num} {sub}'},
                    ]}

        def legend_col(key, align):
            color_map = {"0-1人": "#22c55e", "2-3人": "#3b82f6", "4-5人": "#ef4444"}
            sq = color_map.get(key, "#999")
            return {'component': 'VCol', 'props': {'cols': 12, 'md': 4},
                    'content': [{'component': 'div',
                                 'props': {'style': f'display:flex;align-items:center;justify-content:{align == "left" and "flex-start" or align == "right" and "flex-end" or "center"};font-size:11px;color:rgba(var(--v-theme-on-surface),.6);white-space:nowrap;'},
                                 'content': [
                                     {'component': 'div', 'props': {'style': f'width:8px;height:8px;background:{sq};margin-right:4px;flex-shrink:0;'}, 'text': ''},
                                     {'component': 'span', 'props': {}, 'text': f'{key} {d[key]["count"]}个 {d[key]["gb"]:.1f}GB'},
                                 ]}]}

        last_bean = ov.get("last_bean")
        last_pt = ov.get("last_pt")
        last_date = ov.get("last_date", "")
        bean_txt = f'{last_bean:.0f}' if isinstance(last_bean, (int, float)) else '—'
        pt_txt = f'{last_pt:.0f}' if isinstance(last_pt, (int, float)) else '—'

        return {'component': 'VCard', 'props': {'flat': True},
                'content': [
                    {'component': 'VCardText', 'props': {'class': 'pt-2'},
                     'content': [
                         {'component': 'VRow', 'content': [
                             stat_col("🌱 在保种子", ov.get("count", 0), "个"),
                             stat_col("💾 总体积", f'{ov.get("total_gb", 0.0):.1f}', "GB"),
                             stat_col("🥜 上次憨豆", bean_txt, ""),
                             stat_col("⭐ 上次积分", pt_txt, ""),
                         ]},
                         {'component': 'div',
                          'props': {'style': 'margin-top:8px;margin-bottom:4px;font-size:11px;color:rgba(var(--v-theme-on-surface),.6);'},
                          'text': f'初始做种人数分布' + (f' · 上次结算 {last_date}' if last_date else '')},
                         {'component': 'div',
                          'props': {'style': 'width:100%;display:flex;border-radius:4px;overflow:hidden;'},
                          'content': [
                              {'component': 'div', 'props': {'style': f'height:8px;width:{c01:.2f}%;background:#22c55e;'}, 'text': ''},
                              {'component': 'div', 'props': {'style': f'height:8px;width:{c23:.2f}%;background:#3b82f6;'}, 'text': ''},
                              {'component': 'div', 'props': {'style': f'height:8px;width:{c45:.2f}%;background:#ef4444;'}, 'text': ''},
                          ]},
                         {'component': 'VRow', 'props': {'no-gutters': True}, 'content': [
                             legend_col("0-1人", "left"),
                             legend_col("2-3人", "center"),
                             legend_col("4-5人", "right"),
                         ]},
                         {'component': 'VRow', 'props': {'class': 'd-flex align-center', 'no-gutters': True, 'style': 'margin-top:8px;'},
                          'content': [
                              {'component': 'VCol', 'props': {'cols': 'auto'},
                               'content': [{'component': 'div',
                                            'props': {'style': 'font-size:10.5px;color:rgba(var(--v-theme-on-surface),.4);'},
                                            'text': HHClubButler._refresh_note(self._last_overview_ts, self._overview_hours)}]},
                              {'component': 'VCol', 'props': {'cols': True},
                               'content': [{'component': 'div',
                                            'props': {'style': 'font-size:10.5px;color:rgba(var(--v-theme-on-surface),.4);text-align:right;'},
                                            'text': '左下角 [查看数据] 内置 [立即刷新] 及 [保种管理器]'}]},
                          ]},
                     ]},
                    {'component': 'VAlert', 'props': {'type': 'info', 'variant': 'tonal',
                                                     'text': f"最近运行状态：{self._last_result}"}}
                ]}

    @staticmethod
    def _overview_html(ov: dict, last_ts: float = 0.0, hours: float = 6.0) -> str:
        if not ov or not ov.get("ok"):
            return ('<div style="padding:12px 14px;border-radius:10px;'
                    'border:1px solid rgba(var(--v-theme-on-surface),.08);'
                    'background:rgba(var(--v-theme-surface),.55);'
                    'color:rgba(var(--v-theme-on-surface),.72);font-size:12px;">'
                    '憨憨保种区管家：未获取到数据（请确认插件已启用、下载器已选择、完成页/下载器可访问）</div>')
        d = ov["dist"]
        t_gb = ov.get("total_gb", 0.0) or 1.0
        C = {"0-1人": "#22c55e", "2-3人": "#3b82f6", "4-5人": "#ef4444"}
        ON = "rgb(var(--v-theme-on-surface))"
        ON60 = "rgba(var(--v-theme-on-surface),.6)"
        ON40 = "rgba(var(--v-theme-on-surface),.4)"

        def pct(k):
            return (d[k]["gb"] / t_gb * 100.0) if t_gb else 0.0

        last_bean = ov.get("last_bean")
        last_pt = ov.get("last_pt")
        last_date = ov.get("last_date", "")
        bean_txt = f'{last_bean:.0f}' if isinstance(last_bean, (int, float)) else '—'
        pt_txt = f'{last_pt:.0f}' if isinstance(last_pt, (int, float)) else '—'

        def td(label, num, sub):
            return (f'<td style="text-align:center;padding:4px 2px;width:25%;vertical-align:top;">'
                    f'<div style="font-size:10.5px;color:{ON60};white-space:nowrap;">{label}</div>'
                    f'<div style="font-size:17px;font-weight:700;color:{ON};line-height:1.4;white-space:nowrap;">{num}'
                    f'<span style="font-size:10px;font-weight:400;color:{ON40};"> {sub}</span></div></td>')

        cells = ('<table style="width:100%;border-collapse:collapse;table-layout:fixed;margin:2px 0 6px;"><tr>'
                 + td("🌱 在保种子", f'{ov["count"]}', "个")
                 + td("💾 总体积", f'{ov["total_gb"]:.1f}', "GB")
                 + td("🥜 上次憨豆", bean_txt, "")
                 + td("⭐ 上次积分", pt_txt, "")
                 + '</tr></table>')
        bar = ('<table style="width:100%;border-collapse:collapse;table-layout:fixed;margin:6px 0 6px;"><tr>'
               f'<td style="height:10px;width:{pct("0-1人"):.2f}%;background:{C["0-1人"]};border-radius:5px 0 0 5px;"></td>'
               f'<td style="height:10px;width:{pct("2-3人"):.2f}%;background:{C["2-3人"]};"></td>'
               f'<td style="height:10px;width:{pct("4-5人"):.2f}%;background:{C["4-5人"]};border-radius:0 5px 5px 0;"></td>'
               '</tr></table>')
        legend = ('<table style="width:100%;border-collapse:collapse;table-layout:fixed;font-size:10.5px;color:{ON60};"><tr>'
                  f'<td style="text-align:left;white-space:nowrap;"><span style="display:inline-block;width:7px;height:7px;border-radius:2px;background:{C["0-1人"]};margin-right:3px;"></span>0-1人 {d["0-1人"]["count"]}个 {d["0-1人"]["gb"]:.1f}GB</td>'
                  f'<td style="text-align:center;white-space:nowrap;"><span style="display:inline-block;width:7px;height:7px;border-radius:2px;background:{C["2-3人"]};margin-right:3px;"></span>2-3人 {d["2-3人"]["count"]}个 {d["2-3人"]["gb"]:.0f}GB</td>'
                  f'<td style="text-align:right;white-space:nowrap;"><span style="display:inline-block;width:7px;height:7px;border-radius:2px;background:{C["4-5人"]};margin-right:3px;"></span>4-5人 {d["4-5人"]["count"]}个 {d["4-5人"]["gb"]:.1f}GB</td>'
                  '</tr></table>')
        dist_title = (f'<table style="width:100%;border-collapse:collapse;table-layout:fixed;margin:2px 0 2px;"><tr>'
                      f'<td style="text-align:left;font-size:10.5px;color:{ON60};">初始做种人数分布'
                      + (f' · 上次结算 {last_date}' if last_date else '')
                      + '</td></tr></table>')
        return f'<div style="background:transparent;">{cells}{dist_title}{bar}{legend}</div>'

    @staticmethod
    def _refresh_note(last_ts: float = 0.0, hours: float = 6.0) -> str:
        if last_ts and last_ts > 0:
            mins = max(0, int((time.time() - last_ts) / 60))
            if mins >= 60:
                h, m = divmod(mins, 60)
                ago = f"{h} 小时 {m} 分钟前" if m else f"{h} 小时前"
            else:
                ago = f"{mins} 分钟前"
            if hours > 0:
                return f"每 {hours:g} 小时自动刷新一次 · 上次更新 {ago}"
            return f"上次更新 {ago}"
        return f"每 {hours:g} 小时自动刷新一次"

    def _build_management_page(self) -> dict:
        """仿QB风格保种管理页"""
        try:
            cur = self._get_current_seeding([])
        except Exception as e:
            cur = {"error": str(e), "seeds": []}
        seeds = cur.get("seeds", []) or []
        seeds = sorted(seeds, key=lambda s: (tier_of(s.get("seeders", 1)), -s.get("size", 0)))

        rows = []
        for i, s in enumerate(seeds, 1):
            title = (s.get("title") or "").strip()
            size = s.get("size", 0.0) or 0.0
            seeders = s.get("seeders", 0) or 0
            t = tier_of(seeders)
            badge = TIER_BADGE.get(t, '<span style="display:inline-block;padding:1px 8px;border-radius:10px;background:#999;color:#fff;font-size:11px;">&gt;5人</span>')
            safe_title = title.replace("\\", "\\\\").replace("'", "\\'")
            rows.append({
                "component": "VRow",
                "props": {"class": "py-1", "style": "border-bottom:1px solid rgba(var(--v-theme-on-surface),.06);"},
                "content": [
                    {"component": "VCol", "props": {"cols": 1},
                     "content": [{"component": "div",
                                   "props": {"class": "text-caption",
                                             "style": "text-align:center;color:rgba(var(--v-theme-on-surface),.5);"},
                                   "text": str(i)}]},
                    {"component": "VCol", "props": {"cols": 6},
                     "content": [{"component": "div",
                                   "props": {"style": "font-size:12px;word-break:break-all;line-height:1.4;",
                                             "title": title},
                                   "text": title}]},
                    {"component": "VCol", "props": {"cols": 2},
                     "content": [{"component": "div",
                                   "props": {"class": "text-caption", "style": "text-align:right;"},
                                   "text": f"{size:.2f} GB"}]},
                    {"component": "VCol", "props": {"cols": 2},
                     "content": [{"component": "div", "props": {"style": "text-align:center;"},
                                   "html": badge}]},
                    {"component": "VCol", "props": {"cols": 1},
                     "content": [{"component": "VBtn",
                                   "props": {"size": "x-small", "color": "error", "variant": "tonal",
                                             "icon": "mdi-delete-outline"},
                                   "events": {"click": {
                                       "api": "plugin/HHClubButler/delete_rescue_seed",
                                       "method": "post",
                                       "params": {"apikey": settings.API_TOKEN, "title": safe_title}
                                   }}}]},
                ]
            })

        header = {
            "component": "VRow",
            "props": {"class": "py-1",
                      "style": "border-bottom:1px solid rgba(var(--v-theme-on-surface),.12);font-weight:600;font-size:11px;color:rgba(var(--v-theme-on-surface),.6);"},
            "content": [
                {"component": "VCol", "props": {"cols": 1}, "content": [{"component": "div", "text": "#"}]},
                {"component": "VCol", "props": {"cols": 6}, "content": [{"component": "div", "text": "种子名称"}]},
                {"component": "VCol", "props": {"cols": 2},
                 "content": [{"component": "div", "props": {"style": "text-align:right;"}, "text": "大小"}]},
                {"component": "VCol", "props": {"cols": 2},
                 "content": [{"component": "div", "props": {"style": "text-align:center;"}, "text": "档位"}]},
                {"component": "VCol", "props": {"cols": 1},
                 "content": [{"component": "div", "props": {"style": "text-align:center;"}, "text": "删除"}]},
            ]
        }

        total_gb = sum(s.get("size", 0.0) for s in seeds)
        summary_html = (
            f'<div style="padding:6px 4px;font-size:12px;color:rgba(var(--v-theme-on-surface),.6);">'
            f'共 <b style="color:rgb(var(--v-theme-on-surface));">{len(seeds)}</b> 个种子 · '
            f'总体积 <b style="color:rgb(var(--v-theme-on-surface));">{total_gb:.1f} GB</b> · '
            f'点击删除按钮将同时删除下载器任务和文件</div>'
        )

        return {
            "component": "VCard",
            "props": {"flat": True},
            "content": [
                {"component": "VCardTitle", "props": {"class": "text-subtitle-1"},
                 "content": [{"component": "div", "text": "📋 保种管理（仿QB面板）"}]},
                {"component": "VCardText", "content": [
                    {"component": "div", "html": summary_html},
                    header,
                    *rows,
                    {"component": "div", "props": {"style": "margin-top:8px;"},
                     "content": [{"component": "VBtn",
                                   "props": {"size": "small", "color": "primary", "variant": "tonal",
                                             "prepend-icon": "mdi-refresh"},
                                   "events": {"click": {
                                       "api": "plugin/HHClubButler/refresh_overview",
                                       "method": "get",
                                       "params": {"apikey": settings.API_TOKEN}
                                   }},
                                   "content": [{"component": "div", "text": "刷新列表"}]}]}
                ]}
            ]
        }

    def get_page(self) -> List[dict]:
        overview = None
        try:
            overview = self._get_overview_cached_or_refresh()
        except Exception as e:
            logger.error(f"获取憨憨保种区管家概况失败：{e}")
        panel_link = (
            '<div style="padding:10px 0;display:flex;align-items:center;gap:10px;">'
            '<a href="/api/v1/plugin/HHClubButler/rescue_panel?apikey=' + getattr(settings, 'API_TOKEN', '') + '" target="_blank" '
            'style="display:inline-flex;align-items:center;gap:6px;padding:7px 18px;background:rgba(66,165,245,.12);'
            'color:#42A5F5;text-decoration:none;border-radius:4px;font-size:13px;font-weight:500;border:1px solid rgba(66,165,245,.3);">'
            '🖥️ 保种管理器</a>'
            '<span style="font-size:11px;color:rgba(var(--v-theme-on-surface),.45);">独立工作台 · 批量删除 · 搜索· 查看· 档位· 筛选· 列自定义 · 多皮肤等功能</span>'
            '</div>'
        )
        return [{
            'component': 'VRow', 'props': {'density': 'compact', 'no-gutters': True},
            'content': [
                {'component': 'VCol', 'props': {'cols': 12, 'md': 12},
                 'content': [
                     {'component': 'VRow', 'props': {'class': 'd-flex justify-space-between align-center', 'no-gutters': True},
                      'content': [
                          {'component': 'div', 'html': (
                              '<div style="font-size:10.5px;color:rgba(var(--v-theme-on-surface),.4);'
                              'margin-right:10px;white-space:nowrap;">'
                              + HHClubButler._refresh_note(self._last_overview_ts, self._overview_hours)
                              + '</div>')},
                          {'component': 'div', 'html': '<a style="cursor:pointer;color:#42A5F5;text-decoration:none;white-space:nowrap;">🔄 立即刷新</a>',
                           'events': {'click': {'api': 'plugin/HHClubButler/refresh_overview', 'method': 'get',
                                                'params': {'apikey': settings.API_TOKEN}}}}
                      ]},
                     {'component': 'div', 'html': HHClubButler._overview_html(
                         overview, self._last_overview_ts, self._overview_hours)},
                     {'component': 'VAlert', 'props': {'type': 'info', 'variant': 'tonal',
                                                      'text': f"最近运行状态：{self._last_result}"}},
                     {'component': 'div', 'html': panel_link},
                 ]},
            ]
        }]

    def get_dashboard_meta(self) -> Optional[List[Dict[str, str]]]:
        return [{"key": "seeding", "name": "憨憨保种区管家"}]

    def get_dashboard(self, key: str, **kwargs):
        if key and key != "seeding":
            return None
        try:
            overview = self._get_overview_cached_or_refresh()
        except Exception as e:
            logger.error(f"仪表盘概况获取失败：{e}")
            overview = {"count": 0, "total_gb": 0.0,
                        "dist": {"0-1人": {"count": 0, "gb": 0.0},
                                 "2-3人": {"count": 0, "gb": 0.0},
                                 "4-5人": {"count": 0, "gb": 0.0}}}
        elements = [
            {'component': 'div',
             'props': {'style': 'text-align:left;font-size:10.5px;color:rgba(var(--v-theme-on-surface),.4);margin-bottom:6px;white-space:nowrap;'},
             'text': HHClubButler._refresh_note(self._last_overview_ts, self._overview_hours)},
            {'component': 'div', 'html': HHClubButler._overview_html(
                overview, self._last_overview_ts, self._overview_hours)}
        ]
        return ({"cols": 12, "md": 6},
                {"refresh": 60, "title": "憨憨保种区管家", "border": True},
                elements)

    def stop_service(self):
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
        except Exception as e:
            logger.error(f"停止插件服务失败：{e}")
        try:
            if self._overview_stop:
                self._overview_stop.set()
            if self._overview_thread and self._overview_thread.is_alive():
                self._overview_thread.join(timeout=3)
        except Exception as e:
            logger.error(f"停止概况刷新线程失败：{e}")
        finally:
            self._overview_stop = None
            self._overview_thread = None

    def _start_overview_thread(self):
        try:
            self._overview_stop = threading.Event()
            self._overview_thread = threading.Thread(
                target=self._overview_loop, daemon=True, name="HHClubButler-OverviewRefresh")
            self._overview_thread.start()
        except Exception as e:
            logger.error(f"启动概况刷新线程失败：{e}")

    def _overview_loop(self):
        while True:
            try:
                if self._overview_stop is None or self._overview_stop.is_set():
                    return
                hours = self._overview_hours
                if hours <= 0:
                    self._overview_stop.wait(3600)
                    continue
                wait = hours * 3600.0 - (time.time() - self._last_overview_ts)
                if wait <= 0:
                    self._refresh_overview()
                    wait = hours * 3600.0
                self._overview_stop.wait(min(wait, 3600.0))
            except Exception:
                try:
                    if self._overview_stop is not None:
                        self._overview_stop.wait(300)
                except Exception:
                    return

    def _refresh_overview(self, manual: bool = False):
        try:
            cur = self._get_current_seeding([])
            if cur.get("error"):
                logger.warning(f"憨憨保种区管家概况刷新失败（{cur['error']}），保留上次数据")
                return
            if cur.get("degraded"):
                logger.warning(f"憨憨保种区管家概况刷新失败（{cur['degraded']}），保留上次数据")
                return
            self._last_overview = self._build_overview_from_current(cur)
            self._last_overview_ts = time.time()
            logger.info(f"憨憨保种区管家概况已{'手动' if manual else '自动'}刷新："
                        f"{cur.get('count', 0)} 个 / {cur.get('total_gb', 0.0):.1f} GB"
                        f" / 上次憨豆 {self._last_overview.get('last_bean', '—')}"
                        f" / 上次积分 {self._last_overview.get('last_pt', '—')}")
        except Exception as e:
            logger.error(f"概况{'手动' if manual else '自动'}刷新失败：{e}")

    def _get_overview_cached_or_refresh(self) -> dict:
        try:
            if (self._last_overview is None
                    or (self._overview_hours > 0
                        and time.time() - self._last_overview_ts > self._overview_hours * 3600 + 300)):
                self._refresh_overview()
        except Exception as e:
            logger.error(f"概况读取失败：{e}")
        return self._last_overview or {
            "count": 0, "total_gb": 0.0,
            "dist": {"0-1人": {"count": 0, "gb": 0.0},
                     "2-3人": {"count": 0, "gb": 0.0},
                     "4-5人": {"count": 0, "gb": 0.0}}}

    def api_run(self):
        if self._running:
            return {"success": False, "result": "憨憨保种区管家正在运行中，请稍候"}
        threading.Thread(target=self.run, daemon=True).start()
        return {"success": True, "result": "已在后台启动优选，请查看运行日志"}

    def api_result(self):
        return {"result": self._last_result}

    def api_refresh_overview(self):
        try:
            self._refresh_overview(manual=True)
            if self._last_overview_ts > 0:
                return {"success": True, "message": "概况已刷新", "data": None}
            return {"success": False, "message": "概况刷新失败", "data": None}
        except Exception as e:
            logger.error(f"立即刷新概况失败：{e}")
            return {"success": False, "message": f"概况刷新失败：{e}", "data": None}

    def api_list_rescue_seeds(self):
        try:
            cur = self._get_current_seeding([])
            seeds = list(cur.get("seeds", []) or [])
            try:
                detail = self._get_downloader_detail_map()
            except Exception as e:
                logger.warning(f"下载器详情合并失败：{e}")
                detail = {}
            for s in seeds:
                nm = HHClubButler._norm_title(s.get("title") or "")
                d = detail.get(nm) or {}
                for k in ("upspeed", "dlspeed", "eta", "ratio", "path",
                          "cur_seeders", "cur_leechers", "tracker", "added",
                          "progress", "hash"):
                    if k in d:
                        s[k] = d[k]
            return {"success": True, "data": seeds}
        except Exception as e:
            logger.error(f"list_rescue_seeds 失败：{e}")
            return {"success": False, "message": str(e)}

    def _get_downloader_detail_map(self) -> dict:
        out = {}
        service = self._get_downloader_obj()
        if not service:
            return out
        try:
            dl_type = str(service.type or service.config.type or "").lower()
        except Exception:
            dl_type = ""
        try:
            torrents, error = service.instance.get_torrents()
            if error:
                return out
        except Exception as e:
            logger.warning(f"详情map取种子失败：{e}")
            return out

        def g(t, *names):
            for n in names:
                try:
                    v = t.get(n) if isinstance(t, dict) else getattr(t, n, None)
                    if v is not None:
                        return v
                except Exception:
                    pass
            return None

        for t in torrents:
            try:
                name = g(t, "name") or ""
                if not name:
                    continue
                progress = HHClubButler._get_progress_ratio(t, dl_type)
                if progress is not None and progress < 1.0:
                    continue
                upspeed = g(t, "upspeed", "rateUpload", "upload_speed", "uploadRate") or 0
                dlspeed = g(t, "dlspeed", "rateDownload", "download_speed", "downloadRate") or 0
                eta = g(t, "eta") or 0
                ratio = g(t, "ratio", "uploadRatio")
                path = g(t, "save_path", "downloadDir", "download_dir", "content_path", "path", "dir") or ""
                cur_s = g(t, "num_seeds", "seeds", "seeders", "seederCount", "num_seeders")
                cur_l = g(t, "num_leechs", "leechs", "leechers", "leecherCount", "num_leechers")
                added = g(t, "added_on", "date_added", "dateAdded")
                tracker = ""
                try:
                    tracker = HHClubButler._extract_tracker_text(t, dl_type)
                except Exception:
                    pass
                dom = ""
                m = re.search(r"https?://([^/]+)", tracker or "")
                if m:
                    dom = m.group(1)
                added_str = ""
                try:
                    if added:
                        added = int(added)
                        if added > 1e9:
                            added_str = time.strftime("%Y-%m-%d", time.localtime(added))
                except Exception:
                    pass
                out[HHClubButler._norm_title(name)] = {
                    "upspeed": float(upspeed or 0),
                    "dlspeed": float(dlspeed or 0),
                    "eta": int(eta or 0),
                    "ratio": float(ratio) if ratio is not None else None,
                    "path": str(path or ""),
                    "cur_seeders": int(cur_s) if cur_s is not None else None,
                    "cur_leechers": int(cur_l) if cur_l is not None else None,
                    "tracker": dom,
                    "added": added_str,
                    "progress": progress,
                }
            except Exception:
                continue
        return out

    def api_rescue_panel(self):
        apikey = getattr(settings, "API_TOKEN", "") or ""
        return HTMLResponse(content=self._build_rescue_panel_html(apikey))

    def _build_rescue_panel_html(self, apikey: str) -> str:
        import json as _json
        ak = _json.dumps(apikey)
        html = r"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>憨憨保种区管理</title><style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:"Segoe UI","PingFang SC",Arial,sans-serif;background:var(--bg);color:var(--fg);font-size:12px;height:100vh;display:flex;flex-direction:column;overflow:hidden}
body[data-theme="light"]{--bg:#ffffff;--fg:#1d1d1f;--topbar:#f5f5f7;--border:#e5e5ea;--chip:#e8e8ed;--chip-active:#0071e3;--chip-hover:#d1d1d6;--input-bg:#ffffff;--input-border:#d2d2d7;--btn:#0071e3;--btn-danger:#ff3b30;--btn-ghost:#e8e8ed;--toolbar:#f0f0f2;--picker-bg:#ffffff;--picker-border:#d2d2d7;--th:#f5f5f7;--th-hover:#e8e8ed;--td-border:#f0f0f2;--tr-hover:#f0f5ff;--tr-sel:#0071e3;--footer:#f5f5f7;--footer-border:#e5e5ea;--muted:#86868b;--muted2:#6e6e73}
body[data-theme="cream"]{--bg:#fdf6e3;--fg:#3e2f1c;--topbar:#f5e6c8;--border:#d4c5a0;--chip:#ead9b8;--chip-active:#b8860b;--chip-hover:#e0cf9f;--input-bg:#fff8e0;--input-border:#c4b080;--btn:#b8860b;--btn-danger:#c0392b;--btn-ghost:#ead9b8;--toolbar:#f0e0bc;--picker-bg:#fdf6e3;--picker-border:#c4b080;--th:#f0e0bc;--th-hover:#e8d5a0;--td-border:#e0d0a5;--tr-hover:#f5e6b8;--tr-sel:#8b6914;--footer:#e8d8a8;--footer-border:#d4c5a0;--muted:#8b7355;--muted2:#6b5535}
body[data-theme="sakura"]{--bg:#fff5f7;--fg:#4a2a35;--topbar:#ffe4ec;--border:#f8c8d8;--chip:#ffd6e5;--chip-active:#e91e63;--chip-hover:#ffc2d6;--input-bg:#ffffff;--input-border:#f8b8cc;--btn:#e91e63;--btn-danger:#c0392b;--btn-ghost:#ffd6e5;--toolbar:#ffe0ec;--picker-bg:#fff5f7;--picker-border:#f8b8cc;--th:#ffe0ec;--th-hover:#ffd0e0;--td-border:#f8d0dc;--tr-hover:#ffe4ef;--tr-sel:#ad1457;--footer:#ffe4ec;--footer-border:#f8c8d8;--muted:#a06878;--muted2:#804858}
body[data-theme="mint"]{--bg:#f0faf4;--fg:#1f3a2e;--topbar:#d8f0e2;--border:#b8dcc4;--chip:#c8ecd4;--chip-active:#10b981;--chip-hover:#b0e4c2;--input-bg:#ffffff;--input-border:#a8d4b8;--btn:#10b981;--btn-danger:#e53e3e;--btn-ghost:#c8ecd4;--toolbar:#e0f2e8;--picker-bg:#f0faf4;--picker-border:#a8d4b8;--th:#e0f2e8;--th-hover:#d0ead8;--td-border:#d8ecd0;--tr-hover:#e0f5e8;--tr-sel:#059669;--footer:#e0f2e8;--footer-border:#b8dcc4;--muted:#6a9a7e;--muted2:#4a7a5e}
body[data-theme="sky"]{--bg:#f0f7ff;--fg:#1a2a3e;--topbar:#dceafd;--border:#b8d4f0;--chip:#cce4fa;--chip-active:#0ea5e9;--chip-hover:#b8d8f5;--input-bg:#ffffff;--input-border:#a8c8ee;--btn:#0ea5e9;--btn-danger:#e53e3e;--btn-ghost:#cce4fa;--toolbar:#e0eefd;--picker-bg:#f0f7ff;--picker-border:#a8c8ee;--th:#e0eefd;--th-hover:#d0e6f8;--td-border:#d8e8f8;--tr-hover:#e0f0ff;--tr-sel:#0284c7;--footer:#e0eefd;--footer-border:#b8d4f0;--muted:#6a8aaa;--muted2:#4a6a8a}
body[data-theme="lavender"]{--bg:#f6f3ff;--fg:#2e1f4a;--topbar:#e8e0fb;--border:#ccbfe8;--chip:#ddd2f5;--chip-active:#8b5cf6;--chip-hover:#cdbef0;--input-bg:#ffffff;--input-border:#bcade0;--btn:#8b5cf6;--btn-danger:#e53e3e;--btn-ghost:#ddd2f5;--toolbar:#ece6fb;--picker-bg:#f6f3ff;--picker-border:#bcade0;--th:#ece6fb;--th-hover:#e0d6f5;--td-border:#e0d8f0;--tr-hover:#ece4ff;--tr-sel:#7c3aed;--footer:#ece6fb;--footer-border:#ccbfe8;--muted:#8a7aaa;--muted2:#6a5a8a}
body[data-theme="silver"]{--bg:#eef0f2;--fg:#2a2d32;--topbar:#dde1e6;--border:#c0c5cc;--chip:#d4d8dd;--chip-active:#6b7280;--chip-hover:#c8ccd2;--input-bg:#ffffff;--input-border:#b8bdc4;--btn:#6b7280;--btn-danger:#d97706;--btn-ghost:#d4d8dd;--toolbar:#e4e7eb;--picker-bg:#eef0f2;--picker-border:#b8bdc4;--th:#e4e7eb;--th-hover:#d8dce0;--td-border:#dde1e6;--tr-hover:#e2e6ea;--tr-sel:#4b5563;--footer:#dde1e6;--footer-border:#c0c5cc;--muted:#7a8088;--muted2:#5a6068}
body[data-theme="dark"]{--bg:#1e1e1e;--fg:#d4d4d4;--topbar:#252526;--border:#000;--chip:#3a3d41;--chip-active:#0e639c;--chip-hover:#494c50;--input-bg:#3c3c3c;--input-border:#555;--btn:#0e639c;--btn-danger:#c72e2e;--btn-ghost:#3a3d41;--toolbar:#2d2d30;--picker-bg:#252526;--picker-border:#555;--th:#2d2d30;--th-hover:#37373d;--td-border:#333;--tr-hover:#2a2d2e;--tr-sel:#094771;--footer:#000;--footer-border:#000;--muted:#888;--muted2:#aaa}
body[data-theme="graphite"]{--bg:#2c2c2e;--fg:#e5e5e7;--topbar:#1c1c1e;--border:#0a0a0a;--chip:#3a3a3c;--chip-active:#64d2ff;--chip-hover:#48484a;--input-bg:#1c1c1e;--input-border:#48484a;--btn:#64d2ff;--btn-danger:#ff453a;--btn-ghost:#3a3a3c;--toolbar:#1c1c1e;--picker-bg:#2c2c2e;--picker-border:#48484a;--th:#1c1c1e;--th-hover:#3a3a3c;--td-border:#38383a;--tr-hover:#3a3a3c;--tr-sel:#0a84ff;--footer:#000;--footer-border:#000;--muted:#8e8e93;--muted2:#aeaeb2}
body[data-theme="midnight"]{--bg:#191923;--fg:#e0e0ec;--topbar:#10101a;--border:#050508;--chip:#2a2a38;--chip-active:#5e5ce6;--chip-hover:#35354a;--input-bg:#10101a;--input-border:#3a3a4e;--btn:#5e5ce6;--btn-danger:#ff453a;--btn-ghost:#2a2a38;--toolbar:#14141e;--picker-bg:#191923;--picker-border:#3a3a4e;--th:#14141e;--th-hover:#20202e;--td-border:#252535;--tr-hover:#22222e;--tr-sel:#4846c9;--footer:#050508;--footer-border:#000;--muted:#6a6a8a;--muted2:#9a9ab8}
body[data-theme="navy"]{--bg:#0d1b2a;--fg:#c8d6e5;--topbar:#1b263b;--border:#0a1520;--chip:#2c3e5d;--chip-active:#4fc3f7;--chip-hover:#34496b;--input-bg:#1b263b;--input-border:#34496b;--btn:#4fc3f7;--btn-danger:#e74c3c;--btn-ghost:#2c3e5d;--toolbar:#162235;--picker-bg:#0d1b2a;--picker-border:#34496b;--th:#162235;--th-hover:#1f2f4a;--td-border:#1f2d42;--tr-hover:#1a2a42;--tr-sel:#2a6fa0;--footer:#0a1520;--footer-border:#000;--muted:#6a8aa8;--muted2:#8aa8c8}
body[data-theme="plum"]{--bg:#1a0f1f;--fg:#e0c8e8;--topbar:#2a1530;--border:#100a15;--chip:#3a2045;--chip-active:#ce93d8;--chip-hover:#452850;--input-bg:#2a1530;--input-border:#452850;--btn:#ce93d8;--btn-danger:#ef5350;--btn-ghost:#3a2045;--toolbar:#22122a;--picker-bg:#1a0f1f;--picker-border:#452850;--th:#22122a;--th-hover:#2e1838;--td-border:#2e1838;--tr-hover:#2a1830;--tr-sel:#7b1fa2;--footer:#100a15;--footer-border:#000;--muted:#8a6a9a;--muted2:#b090c0}
body[data-theme="charcoal"]{--bg:#23201e;--fg:#e0dcd6;--topbar:#1a1715;--border:#0a0806;--chip:#3a3532;--chip-active:#d4a574;--chip-hover:#45403c;--input-bg:#1a1715;--input-border:#4a4440;--btn:#d4a574;--btn-danger:#e74c3c;--btn-ghost:#3a3532;--toolbar:#1e1a18;--picker-bg:#23201e;--picker-border:#4a4440;--th:#1e1a18;--th-hover:#2a2522;--td-border:#33302c;--tr-hover:#2a2624;--tr-sel:#8a6a44;--footer:#0a0806;--footer-border:#000;--muted:#8a8078;--muted2:#aaa098}
body[data-theme="forest"]{--bg:#0f1f17;--fg:#c8e0d0;--topbar:#162a1f;--border:#0a1510;--chip:#1f3a2a;--chip-active:#66bb6a;--chip-hover:#2a4a35;--input-bg:#162a1f;--input-border:#2a4a35;--btn:#66bb6a;--btn-danger:#ef5350;--btn-ghost:#1f3a2a;--toolbar:#122519;--picker-bg:#0f1f17;--picker-border:#2a4a35;--th:#122519;--th-hover:#1a3525;--td-border:#1a3020;--tr-hover:#1a2e20;--tr-sel:#2e7d32;--footer:#0a1510;--footer-border:#000;--muted:#5a8a6a;--muted2:#8ab89a}
body[data-theme="hc"]{--bg:#000000;--fg:#ffffff;--topbar:#1a1a1a;--border:#404040;--chip:#2a2a2a;--chip-active:#00ffff;--chip-hover:#3a3a3a;--input-bg:#000;--input-border:#888;--btn:#00ffff;--btn-danger:#ff0000;--btn-ghost:#2a2a2a;--toolbar:#1a1a1a;--picker-bg:#000;--picker-border:#888;--th:#1a1a1a;--th-hover:#2a2a2a;--td-border:#333;--tr-hover:#1a1a1a;--tr-sel:#006666;--footer:#000;--footer-border:#404040;--muted:#aaaaaa;--muted2:#cccccc}
.topbar{background:var(--topbar);padding:8px 12px;display:flex;align-items:center;gap:8px;border-bottom:1px solid var(--border);flex-wrap:wrap;position:relative}
.dot{display:inline-block;width:8px;height:8px;border-radius:0;margin-right:5px;vertical-align:middle}
.chip b{margin-left:3px}
.topbar h1{font-size:14px;color:var(--fg);margin-right:8px}
.chip{background:var(--chip);padding:3px 10px;border-radius:12px;cursor:pointer;font-size:11px;user-select:none;color:var(--fg)}
.chip.active{background:var(--chip-active);color:#fff}
.chip:hover{background:var(--chip-hover)}
.search{margin-left:auto;background:var(--input-bg);border:1px solid var(--input-border);color:var(--fg);padding:5px 10px;border-radius:3px;width:600px;max-width:40vw}
.btn{background:var(--btn);color:#fff;border:none;padding:5px 12px;border-radius:3px;cursor:pointer;font-size:12px}
.btn.danger{background:var(--btn-danger)}
.btn.ghost{background:var(--btn-ghost);color:var(--fg)}
.toolbar2{background:var(--toolbar);padding:6px 12px;display:flex;gap:6px;border-bottom:1px solid var(--border);align-items:center;position:relative;flex-wrap:wrap}
.colpicker{position:absolute;right:12px;top:50px;background:var(--picker-bg);border:1px solid var(--picker-border);border-radius:4px;padding:8px;z-index:100;min-width:160px;display:none;box-shadow:0 4px 16px rgba(0,0,0,0.5);max-height:70vh;overflow:auto}
.colpicker.show{display:block}
.colpicker h4{font-size:11px;color:var(--muted2);margin-bottom:6px;font-weight:normal}
.colpicker label{display:flex;align-items:center;gap:6px;padding:3px 0;cursor:pointer;font-size:12px;color:var(--fg)}
.colpicker label:hover{color:var(--fg)}
.table-wrap{flex:1;overflow:auto}
table{width:100%;border-collapse:separate;border-spacing:0;font-size:12px;table-layout:fixed}
th,td{padding:6px 10px;border-bottom:1px solid var(--td-border);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;position:relative}
th{background:var(--th);color:var(--muted2);text-align:left;font-weight:500;cursor:pointer;user-select:none;position:sticky;top:0;z-index:2}
th:hover{background:var(--th-hover)}
th.sorted{color:var(--muted2)}
th .sortind{margin-left:4px;font-size:10px}
th .resize{position:absolute;right:-3px;top:0;width:9px;height:100%;cursor:col-resize;background:transparent;z-index:3;touch-action:none}
th .resize:hover{background:var(--chip-active)}
tr:hover td{background:var(--tr-hover)}
tr.sel td{background:var(--tr-sel);color:#fff}
.tier0{color:#22c55e;font-weight:600}.tier1{color:#4da3ff;font-weight:600}.tier2{color:#ff6b6b;font-weight:600}
.qual-yes{color:#22c55e}
.right{text-align:right;font-variant-numeric:tabular-nums}
.hide{display:none !important}
.footer{background:var(--footer);padding:6px 14px;font-size:11px;color:var(--muted);border-top:1px solid var(--footer-border);display:flex;gap:18px;flex-wrap:wrap}
.themebtn{background:var(--btn-ghost);color:var(--fg);border:1px solid var(--input-border);padding:3px 10px;border-radius:3px;cursor:pointer;font-size:11px}
.chip .dot{display:inline-block;width:8px;height:8px;border-radius:0;margin-right:4px;vertical-align:middle}
</style></head><body>
<div class="topbar">
<h1>保种管理器</h1>
<span class="chip active" data-tier="all"><i class="dot" style="background:#9e9e9e"></i>全部 <b id="cnt_all">0</b></span>
<span class="chip" data-tier="0"><i class="dot" style="background:#22c55e"></i>0-1人 <b id="cnt_t0">0</b></span>
<span class="chip" data-tier="1"><i class="dot" style="background:#4da3ff"></i>2-3人 <b id="cnt_t1">0</b></span>
<span class="chip" data-tier="2"><i class="dot" style="background:#ff6b6b"></i>4-5人 <b id="cnt_t2">0</b></span>
<input class="search" id="q" placeholder="搜索名称...">
<button class="btn danger" id="btnDel">删除选中</button>
<button class="btn ghost" id="colsBtn">列选择</button>
<select class="themebtn" id="themeSel" title="选择皮肤" style="cursor:pointer;"><option value="light">纯白</option><option value="cream">米黄暖</option><option value="sakura">樱花粉</option><option value="mint">薄荷绿</option><option value="sky">天空蓝</option><option value="lavender">薰衣草紫</option><option value="silver">银灰金属</option><option value="dark">深灰</option><option value="graphite">石墨蓝</option><option value="midnight">午夜蓝</option><option value="navy">深蓝</option><option value="plum">暗紫</option><option value="charcoal">炭灰暖</option><option value="forest">暗夜绿</option><option value="hc">高对比</option></select>
<button class="btn ghost" id="btnExit" title="关闭管理器">退出</button>
<div class="colpicker" id="colpicker"><h4>勾选要显示的列</h4>
<label><input type="checkbox" data-col="c_sid" checked>种子ID</label>
<label><input type="checkbox" data-col="c_name" checked>种子名称</label>
<label><input type="checkbox" data-col="c_size" checked>种子大小</label>
<label><input type="checkbox" data-col="c_init" checked>初始人数</label>
<label><input type="checkbox" data-col="c_now" checked>现在人数</label>
<label><input type="checkbox" data-col="c_tier" checked>档位</label>
<label><input type="checkbox" data-col="c_done_at">完成时间</label>
<label><input type="checkbox" data-col="c_last_settle">上次结算时间</label>
<label><input type="checkbox" data-col="c_today_hours">今日做种时间</label>
<label><input type="checkbox" data-col="c_qualified">今日是否达标</label>
</div>
</div>
<div class="table-wrap"><table id="tbl"><thead><tr id="head"></tr></thead><tbody id="tbody"></tbody></table></div>
<div class="footer">
<span>已选 <b id="seln">0</b> 个 / 共 <b id="totaln">0</b> 个</span>
<span>已选体积 <b id="selgb">0</b> GB</span>
<span>总体积 <b id="totalgb">0</b> GB</span>
</div>
<script>
var APIKEY=__APIKEY__;
var COLS=[
{k:"c_sid",t:"种子ID",w:"70",r:1,sortk:"seed_id"},
{k:"c_name",t:"种子名称",w:"260",sortk:"title"},
{k:"c_size",t:"种子大小",w:"90",r:1,sortk:"size"},
{k:"c_init",t:"初始人数",w:"70",r:1,sortk:"seeders"},
{k:"c_now",t:"现在人数",w:"70",r:1,sortk:"now_seeders"},
{k:"c_tier",t:"档位",w:"80",sortk:"seeders"},
{k:"c_done_at",t:"完成时间",w:"140",sortk:"completed_at"},
{k:"c_last_settle",t:"上次结算时间",w:"140",sortk:"last_settle"},
{k:"c_today_hours",t:"今日做种时间",w:"90",sortk:"today_hours"},
{k:"c_qualified",t:"今日达标",w:"70",sortk:"qualified"}
];
var seeds=[],filtered=[],sel={};
var curTier="all",sortKey="size",sortDir=-1,query="";
function tierOf(n){n=+n||0;return n<=1?0:(n<=3?1:2)}
function fmtSize(gb){if(!isFinite(gb))return "-";gb=+gb;if(gb>=1024)return(gb/1024).toFixed(2)+" TB";return gb.toFixed(2)+" GB"}
function esc(s){return String(s==null?"":s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;")}
function buildHead(){
var h='<th style="width:28px;"><input type="checkbox" id="chkAll"></th>';
for(var i=0;i<COLS.length;i++){var c=COLS[i];var hide=c.visible===false?" hide":"";
var ind="";if(sortKey===c.sortk)ind=sortDir<0?" \u25BC":" \u25B2";
h+='<th class="'+(c.r?"right ":"")+(sortKey===c.sortk?" sorted":"")+hide+'" data-col="'+c.k+'" data-sortk="'+c.sortk+'" style="width:'+c.w+'px;">'+c.t+'<span class="sortind">'+ind+'</span><span class="resize"></span></th>';}
h+='<th style="width:60px;">操作</th>';
document.getElementById("head").innerHTML=h;bindHead();
}
function bindHead(){
var dragMoved=false;
document.querySelectorAll("th .resize").forEach(function(hd){hd.onmousedown=function(e){e.preventDefault();e.stopPropagation();
var th=hd.parentElement,sx=e.clientX,sw=th.offsetWidth;dragMoved=false;document.body.style.cursor='col-resize';
function mv(ev){var dx=ev.clientX-sx;if(Math.abs(dx)>1)dragMoved=true;var w=Math.max(40,sw+dx);th.style.width=w+"px";var col=th.dataset.col;if(col)document.querySelectorAll('td[data-col="'+col+'"]').forEach(function(td){td.style.width=w+"px"})}
function up(ev){document.removeEventListener("mousemove",mv);document.removeEventListener("mouseup",up);document.body.style.cursor='';if(dragMoved&&ev){ev.stopPropagation();ev.preventDefault()}}
document.addEventListener("mousemove",mv);document.addEventListener("mouseup",up)}});
document.querySelectorAll("th[data-sortk]").forEach(function(th){th.onclick=function(ev){if(dragMoved){dragMoved=false;return}var sk=this.dataset.sortk;if(sortKey===sk){sortDir=-sortDir}else{sortKey=sk;sortDir=-1}doSort();buildHead();render()}});
document.getElementById("chkAll").onchange=function(){var v=this.checked;filtered.forEach(function(s){sel[s.title]=v});render()};
}
function doSort(){
seeds.sort(function(a,b){
var va=a[sortKey]||0,vb=b[sortKey]||0;
if(typeof va==="string")return va.localeCompare(vb)*sortDir;
return(va-vb)*sortDir;
});
}
function cellVal(c,s,t){
if(c.k==="c_sid")return s.seed_id||"-";
if(c.k==="c_name")return '<span title="'+esc(s.title)+'">'+esc(s.title)+"</span>";
if(c.k==="c_size")return fmtSize(s.size);
if(c.k==="c_init")return s.seeders;
if(c.k==="c_now")return s.now_seeders!=null?s.now_seeders:"-";
if(c.k==="c_tier")return '<span class="'+["tier0","tier1","tier2"][t]+'">'+["0-1人","2-3人","4-5人"][t]+"</span>";
if(c.k==="c_done_at")return esc(s.completed_at||"-");
if(c.k==="c_last_settle")return esc(s.last_settle||"-");
if(c.k==="c_today_hours")return esc(s.today_hours||"-");
if(c.k==="c_qualified"){var q=s.qualified||"";var ok=(q.indexOf("\u5df2\u8fbe")>=0||q==="\u662f"||q==="Y"||q==="\u2713"||q==="\u2714"||q.indexOf("\u2714")>=0||q.indexOf("OK")>=0);return '<span class="'+(ok?"qual-yes":"")+'">'+esc(q||"-")+"</span>";}
return "";
}
function render(){
var f=seeds.filter(function(s){if(curTier!=="all"&&String(tierOf(s.seeders))!==curTier)return false;if(query&&(s.title||"").toLowerCase().indexOf(query)<0)return false;return true});
filtered=f;
var html="";
for(var i=0;i<f.length;i++){var s=f[i];var t=tierOf(s.seeders);var seled=!!sel[s.title];
html+='<tr class="'+(seled?"sel":"")+'">';
html+='<td><input type="checkbox" data-idx="'+i+'" '+(seled?"checked":"")+"></td>";
for(var j=0;j<COLS.length;j++){var c=COLS[j];if(c.visible===false)continue;
html+='<td class="'+(c.r?"right ":"")+'" data-col="'+c.k+'">'+cellVal(c,s,t)+"</td>";}
html+='<td><a style="color:#ff8080;cursor:pointer;" data-del="'+i+'">删除</a></td></tr>';}
if(!f.length)html='<tr><td colspan="20" style="text-align:center;padding:40px;color:#888">无匹配种子</td></tr>';
document.getElementById("tbody").innerHTML=html;
document.querySelectorAll("#tbody input[type=checkbox]").forEach(function(cb){cb.onchange=function(){var i=+this.dataset.idx;var t=filtered[i].title;sel[t]=this.checked;renderFooter()}});
document.querySelectorAll("#tbody [data-del]").forEach(function(a){a.onclick=function(){delOne(filtered[+this.dataset.del].title)}});
renderFooter();
}
function renderFooter(){
var sn=0,sgb=0;seeds.forEach(function(s){if(sel[s.title]){sn++;sgb+=+s.size||0}});
document.getElementById("seln").textContent=sn;document.getElementById("selgb").textContent=sgb.toFixed(1);
document.getElementById("totaln").textContent=seeds.length;document.getElementById("totalgb").textContent=seeds.reduce(function(a,b){return a+(+b.size||0)},0).toFixed(1);
var c0=0,c1=0,c2=0;seeds.forEach(function(s){var t=tierOf(s.seeders);if(t===0)c0++;else if(t===1)c1++;else c2++});
document.getElementById("cnt_all").textContent=seeds.length;
document.getElementById("cnt_t0").textContent=c0;document.getElementById("cnt_t1").textContent=c1;document.getElementById("cnt_t2").textContent=c2;
}
async function load(){
try{
var r=await fetch("/api/v1/plugin/HHClubButler/list_rescue_seeds?apikey="+encodeURIComponent(APIKEY));
var j=await r.json();
if(!j.success){alert("加载失败:"+(j.message||""));return}
seeds=j.data||[];doSort();render();
}catch(e){alert("请求失败:"+e)}}
async function delOne(title){
if(!confirm("确认删除种子:\\n"+title+"\\n\\n将同时删除下载器任务和文件!"))return;
await doDel([title])}
async function delSel(){
var ts=seeds.filter(function(s){return sel[s.title]}).map(function(s){return s.title});
if(!ts.length){alert("未选中任何种子");return}
if(!confirm("确认删除选中的 "+ts.length+" 个种子?\\n将同时删除下载器任务和文件!"))return;
await doDel(ts)}
async function doDel(titles){
var okTitles=[];
for(var i=0;i<titles.length;i++){
try{var r=await fetch("/api/v1/plugin/HHClubButler/delete_rescue_seed?apikey="+encodeURIComponent(APIKEY)+"&title="+encodeURIComponent(titles[i]),{method:"POST"});var j=await r.json();if(!j.success)alert("删除失败:"+titles[i]+" -> "+(j.message||""));else{okTitles.push(titles[i])}delete sel[titles[i]]}
catch(e){alert("删除异常:"+titles[i]+" "+e)}}
if(okTitles.length){seeds=seeds.filter(function(s){return okTitles.indexOf(s.title)<0});doSort();render();}
}
document.getElementById("btnDel").onclick=delSel;
document.getElementById("q").oninput=function(){query=this.value.trim().toLowerCase();render()};
document.querySelectorAll(".topbar .chip").forEach(function(c){c.onclick=function(){document.querySelectorAll(".topbar .chip").forEach(function(x){x.classList.remove("active")});this.classList.add("active");curTier=this.dataset.tier;render()}});
var cp=document.getElementById("colpicker");
var LS_KEY="hhclubbutler_colcfg_v1";
function loadColCfg(){
try{
var raw=localStorage.getItem(LS_KEY);
if(!raw)return;
var cfg=JSON.parse(raw);
if(cfg&&typeof cfg==="object"){
for(var i=0;i<COLS.length;i++){if(cfg[COLS[i].k]!==undefined)COLS[i].visible=cfg[COLS[i].k]}
}
}catch(e){}
}
function saveColCfg(){
try{var cfg={};for(var i=0;i<COLS.length;i++)cfg[COLS[i].k]=COLS[i].visible!==false;localStorage.setItem(LS_KEY,JSON.stringify(cfg))}catch(e){}
}
function syncColChecks(){
cp.querySelectorAll("input[type=checkbox]").forEach(function(cb){
for(var i=0;i<COLS.length;i++){if(COLS[i].k===cb.dataset.col){cb.checked=COLS[i].visible!==false;break}}
});
}
document.getElementById("colsBtn").onclick=function(e){e.stopPropagation();syncColChecks();cp.classList.toggle("show")};
document.addEventListener("click",function(e){if(!cp.contains(e.target))cp.classList.remove("show")});
cp.querySelectorAll("input[type=checkbox]").forEach(function(cb){cb.onchange=function(){var col=cb.dataset.col;var def=null;for(var i=0;i<COLS.length;i++)if(COLS[i].k===col)def=COLS[i];if(def){def.visible=cb.checked;saveColCfg();buildHead();render()}}});
loadColCfg();
var THEME_KEY="hhclubbutler_theme_v1";
function applyTheme(t){document.body.setAttribute("data-theme",t);try{localStorage.setItem(THEME_KEY,t)}catch(e){}}
try{var lt=localStorage.getItem(THEME_KEY);if(lt)applyTheme(lt);else applyTheme("dark");}catch(e){applyTheme("dark");}
var sel=document.getElementById("themeSel");
sel.value=document.body.getAttribute("data-theme")||"dark";
sel.onchange=function(){applyTheme(this.value)};
document.getElementById("btnExit").onclick=function(){try{window.close()}catch(e){}setTimeout(function(){window.close()},100);if(!window.closed)alert("濡傛湭鑷姩鍏抽棴锛岃鎵嬪姩鍏抽棴鏈爣绛鹃〉")};
buildHead();load();
</script></body></html>"""
        return html.replace("__APIKEY__", ak)

    def api_delete_rescue_seed(self, title: str = None):
        if not title:
            return {"success": False, "message": "缺少 title 参数"}
        logger.info(f"手动删除请求：{title}")
        try:
            service = self._get_downloader_obj()
            if not service:
                return {"success": False, "message": "未配置有效下载器"}
            torrents, error = service.instance.get_torrents()
            if error:
                return {"success": False, "message": "下载器获取种子列表失败"}
            target_norm = HHClubButler._norm_title(title)
            del_ids = []
            matched_name = None
            for t in torrents:
                try:
                    tname = t.get("name") if isinstance(t, dict) else getattr(t, "name", "")
                    thash = (t.get("hash") if isinstance(t, dict)
                             else getattr(t, "hash", None) or getattr(t, "hashString", ""))
                except Exception:
                    continue
                if HHClubButler._norm_match(tname, {target_norm}):
                    del_ids.append(thash)
                    matched_name = tname
            if not del_ids:
                return {"success": False, "message": f"下载器中未找到匹配种子：{title}"}
            service.instance.delete_torrents(delete_file=True, ids=del_ids)
            logger.info(f"已手动删除种子：{matched_name}（{len(del_ids)}个任务，含文件）")
            try:
                self._refresh_overview(manual=False)
            except Exception:
                pass
            return {"success": True, "message": f"已删除：{matched_name or title}"}
        except Exception as e:
            logger.error(f"手动删除种子失败：{e}")
            return {"success": False, "message": f"删除失败：{e}"}

    def run(self):
        if self._running:
            logger.info("憨憨保种区管家正在运行中，跳过本次触发")
            return
        cookie = self._get_site_cookie()
        if not cookie:
            self._last_result = "未获取到站点Cookie"
            logger.warning(self._last_result)
            return
        self._running = True
        try:
            with lock:
                self._do_run()
        except Exception as e:
            logger.error(f"憨憨保种区管家运行异常：{e}")
            self._last_result = f"运行异常：{e}"
            if self._notify:
                try:
                    self.post_message(mtype=NotificationType.Plugin,
                                      title="【憨憨保种区管家】运行异常", text=str(e))
                except Exception:
                    pass
        finally:
            self._running = False

    def _do_run(self):
        logs = []
        logger.info(f"憨憨保种区管家运行开始（代码版本 v{self.plugin_version}）")
        logs.append("=== 憨憨保种区管家保种优选开始 ===")

        seeds = self._fetch_rescue_all(logs)
        if not seeds:
            self._last_result = "保种区未获取到种子（检查Cookie是否有效）"
            logger.warning(self._last_result)
            self._save_log(logs)
            return
        seeds_full = list(seeds)
        all_site_titles = {HHClubButler._norm_title(s.get("title") or "") for s in seeds}

        if self._exclude_zero:
            before = len(seeds)
            seeds = [s for s in seeds if s.get("seeders", 0) > 0]
            logs.append(f"已排除0做种种子 {before - len(seeds)} 个，剩 {len(seeds)} 个")

        current = self._get_current_seeding(logs)
        if current.get("error"):
            msg = f"无法获取当前保种情况（{current['error']}），为防止误推送已停止运行"
            self._last_result = msg
            logger.warning(msg)
            self._save_log(logs)
            return
        current_gb = current.get("total_gb", 0.0)
        logs.append(f"当前保种：{current.get('count', 0)} 个，体积 {current_gb:.1f} GB")

        logger.info(f"自动清理配置：{self._auto_clean_days:g} 天（{'启用' if self._auto_clean_days > 0 else '未启用'}）")
        cleaned = 0
        if self._auto_clean_days > 0:
            cleaned = self._clean_stale_downloads(logs, seeds, all_site_titles)

        dl_all = self._get_downloader_seeds(logs, only_completed=False, any_tracker=True)
        in_flight_gb = 0.0
        if dl_all is None:
            dl_norm_all = set()
            logs.append("⚠️ 下载器种子列表获取失败，本次暂停推送以防重复")
        else:
            dl_done = self._get_downloader_seeds(logs, only_completed=True, any_tracker=True) or set()
            dl_norm_all = {HHClubButler._norm_title(n) for n in dl_all}
            dl_done_norm = {HHClubButler._norm_title(n) for n in dl_done}
            in_flight_norms = dl_norm_all - dl_done_norm
            if in_flight_norms:
                gb_map = {}
                for s in seeds_full:
                    gb_map[HHClubButler._norm_title(s.get("title") or "")] = s.get("size", 0.0)
                in_flight_gb = sum(gb_map.get(nm, 0.0) for nm in in_flight_norms)
            logs.append(f"在途（下载中未完成）{len(in_flight_norms)} 个，体积 {in_flight_gb:.1f} GB")
            before = len(seeds)
            seeds = [s for s in seeds if HHClubButler._norm_title(s.get("title") or "") not in dl_norm_all]
            removed = before - len(seeds)
            if removed:
                logs.append(f"已剔除 {removed} 个已在下载器中的候选（含下载中），剩 {len(seeds)} 个")

        if not current.get("degraded"):
            try:
                self._last_overview = self._build_overview_from_current(current)
                self._last_overview_ts = time.time()
            except Exception as e:
                logger.error(f"构建概况缓存失败：{e}")

        target = self._target_volume
        committed_gb = current_gb + in_flight_gb
        if target and target > 0:
            if committed_gb >= target - 1e-6 and self._mode != "wash":
                msg = f"当前保种+在途已达标（{committed_gb:.1f} GB ≥ 目标 {target:.0f} GB），本次不推不删"
                logs.append(msg)
                self._last_result = f"已达目标（{committed_gb:.1f}/{target:.0f} GB）"
                logger.info(f"憨憨保种区管家：{msg}")
                self._save_log(logs)
                return
            eff_target = max(0.0, target - committed_gb)
            logs.append(f"目标体积（总保种上限）{target:g}，当前 {current_gb:.1f}，在途 {in_flight_gb:.1f}，"
                        f"剩余可增 {eff_target:.1f} GB")
        else:
            eff_target = 0.0

        if self._seeder_cond:
            rng = HHClubButler._parse_seeder_range(self._seeder_cond)
            if rng is None:
                logs.append(f"做种人数条件无法解析：{self._seeder_cond!r}，按不限处理")
            else:
                before = len(seeds)
                seeds = [s for s in seeds if rng[0] <= s.get("seeders", 0) <= rng[1]]
                logs.append(f"做种人数条件 {rng[0]}~{rng[1]}：候选 {before} → {len(seeds)} 个")

        if self._mode == "wash" and current.get("seeds"):
            result = self._optimize_wash(seeds, current.get("seeds"), eff_target, logs)
        else:
            result = self._optimize_incremental(seeds, eff_target, current_gb, logs)
            result["del_seeds"] = []

        picked = result.get("picked", [])
        if result.get("del_seeds"):
            self._delete_seeds(result["del_seeds"], logs)
            for s in result["del_seeds"]:
                logger.info(f"优选删除种子: {s.get('title','')} | {s.get('size',0.0):.1f} GB | "
                            f"初始做种 {s.get('seeders','?')} 人")

        filtered = 0
        if picked and dl_all is not None:
            before = len(picked)
            picked = [s for s in picked if HHClubButler._norm_title(s["title"]) not in dl_norm_all]
            filtered = before - len(picked)
            if filtered:
                logs.append(f"已过滤 {filtered} 个已在下载器的种子，实际推送 {len(picked)} 个")

        ok_count = fail_count = 0
        fail_list = []
        if picked:
            ok_count, fail_count, fail_list = self._push_seeds(picked, logs)
            for s in picked:
                logger.info(f"优选新增种子: {s.get('title','')} | {s.get('size',0.0):.1f} GB | "
                            f"初始做种 {s.get('seeders','?')} 人")
        else:
            logs.append("无需新增下载")

        if fail_list:
            logs.append(f"❌ 推送失败 {len(fail_list)} 个：")
            for f in fail_list:
                logger.error(f"推送失败种子: {f['title']} | {f['reason']}")
                logs.append(f"   {f['title']}（{f['reason']}）")

        total_gb = sum(s.get("size", 0.0) for s in picked)
        mode_name = "换种" if self._mode == "wash" else "增量"
        summary = (f"{mode_name}优选 {len(picked)} 个 | 新增体积 {total_gb:.1f} GB")
        if result.get("del_seeds"):
            summary += f" | 删除 {len(result['del_seeds'])} 个"
        logs.append("=== " + summary + " ===")
        self._last_result = summary
        self._save_log(logs)
        logger.info(f"憨憨保种区管家优选完成：{summary}")

        if self._notify:
            try:
                notify = self._build_notify(mode_name, current, target, eff_target, picked, total_gb,
                                            filtered, ok_count, fail_count, fail_list, result, cleaned)
                self.post_message(mtype=NotificationType.Plugin,
                                  title=f"【憨憨保种区管家】{mode_name}优选完成",
                                  text="\n".join(notify))
            except Exception as e:
                logger.error(f"憨憨保种区管家发送通知失败：{e}")

        del_done = result.get("del_seeds") or []
        if del_done and not current.get("degraded"):
            del_titles = {s.get("title") for s in del_done}
            remain = [s for s in current.get("seeds", []) if s.get("title") not in del_titles]
            current["seeds"] = remain
            current["count"] = len(remain)
            current["total_gb"] = sum(s.get("size", 0.0) for s in remain)
            try:
                self._last_overview = self._build_overview_from_current(current)
                self._last_overview_ts = time.time()
            except Exception as e:
                logger.error(f"删除后更新概况失败：{e}")

    def _build_notify(self, mode_name, current, target, eff_target, picked, total_gb,
                      filtered, ok_count, fail_count, fail_list, result, cleaned=0) -> list:
        lines = ["──────────────"]
        lines.append(f"在保种子：{current.get('count', 0)} 个 / {current.get('total_gb', 0.0):.1f} GB")
        if target and target > 0:
            lines.append(f"目标体积：{target:.0f} GB（剩余可增 {eff_target:.1f} GB）")
        else:
            lines.append("目标体积：不限")
        ov = self._last_overview or {}
        # 档位分布直接从本次运行最新种子数据计算，不使用缓存
        fresh_seeds = current.get("seeds", []) or []
        dist = {"0-1人": {"count": 0, "gb": 0.0},
                "2-3人": {"count": 0, "gb": 0.0},
                "4-5人": {"count": 0, "gb": 0.0}}
        for s in fresh_seeds:
            t = tier_of(s.get("seeders", 0))
            if t > 2:
                t = 2
            dist[TIER_NAMES[t]]["count"] += 1
            dist[TIER_NAMES[t]]["gb"] += s.get("size", 0.0)
        if fresh_seeds:
            lines.append("初始做种人数分布")
            for key in ("0-1人", "2-3人", "4-5人"):
                d = dist.get(key) or {}
                lines.append(f"  {key}  {d.get('count', 0)}个  {d.get('gb', 0.0):.1f} GB")
        last_bean = ov.get("last_bean")
        last_pt = ov.get("last_pt")
        last_date = ov.get("last_date", "") or ""
        if isinstance(last_bean, (int, float)) and isinstance(last_pt, (int, float)):
            seg = f"上次结算：憨豆 {last_bean:.0f}  积分 {last_pt:.0f}"
            if last_date:
                seg += f"（{last_date}）"
            lines.append(seg)
        lines.append("──────────────")
        if picked:
            lines.append(f"新增 {len(picked)} 个种子：+{total_gb:.1f} GB")
            for s in picked[:5]:
                t = (s.get("title") or "").strip()
                t = t if len(t) <= 36 else t[:36] + "…"
                lines.append(f"  · {t}")
            if len(picked) > 5:
                lines.append(f"  …等共 {len(picked)} 个")
            if fail_count:
                lines.append(f"⚠️ 推送失败：{fail_count} 个（成功 {ok_count}）")
            else:
                lines.append(f"推送成功：{ok_count}/{len(picked)}")
        else:
            lines.append("无需新增下载")
            if filtered > 0:
                lines.append(f"已过滤 {filtered} 个已在下载器的种子")
        if result.get("del_seeds"):
            lines.append(f"删除低档位种子：{len(result['del_seeds'])} 个")
            for s in result["del_seeds"][:5]:
                t = (s.get("title") or "").strip()
                t = t if len(t) <= 32 else t[:32] + "…"
                lines.append(f"  - {t}（做种{s.get('seeders','?')}人/{s.get('size',0.0):.0f}GB）")
        if cleaned > 0:
            lines.append(f"清理超时未完成：{cleaned} 个")
        return lines

    def _get_mp_site(self):
        try:
            site_oper = SiteOper()
            for site in site_oper.list_active():
                url = site.url or ""
                domain = site.domain or ""
                if "hhanclub" in url.lower() or "hhclub" in url.lower() \
                        or "hhanclub" in domain.lower() or "hhclub" in domain.lower():
                    return site
        except Exception as e:
            logger.error(f"读取MP站点配置失败：{e}")
        return None

    def _get_site_cookie(self) -> str:
        if not self._cookie:
            site = self._get_mp_site()
            if site and site.cookie:
                self._cookie = site.cookie
                logger.info("已从MP站点管理自动获取憨憨站Cookie")
                return self._cookie
        return self._cookie or ""

    def _get_site_url(self) -> str:
        url = self._site_url
        if not url or url == "https://hhanclub.net":
            site = self._get_mp_site()
            if site and site.url:
                url = site.url.rstrip("/")
        return url

    def _update_cfg(self, **kwargs):
        cfg = self.get_config() or {}
        if not isinstance(cfg, dict):
            cfg = {}
        cfg.update(kwargs)
        self.update_config(cfg)

    def _get_site_uid(self) -> Optional[str]:
        if self._uid:
            return self._uid
        try:
            session = self._session()
            r = session.get(self._get_site_url(), timeout=15)
            m = re.search(r"userdetails\.php\?id=(\d+)", r.text)
            if m:
                uid = m.group(1)
                if self._uid != uid:
                    self._uid = uid
                    try:
                        self._update_cfg(uid=uid)
                    except Exception:
                        pass
                logger.info(f"已从站点主页自动获取UID：{uid}")
                return uid
        except Exception as e:
            logger.error(f"自动获取站点UID失败：{e}")
        logger.warning("未自动获取到站点UID，请检查站点Cookie/UA是否有效")
        return None

    def _session(self) -> requests.Session:
        s = requests.Session()
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        }
        site = self._get_mp_site()
        if site and site.ua:
            headers["User-Agent"] = site.ua
        cookie = self._get_site_cookie()
        if cookie:
            headers["Cookie"] = cookie
        s.headers.update(headers)
        return s

    def _fetch_last_settlement(self) -> Tuple[Optional[float], Optional[float], str]:
        uid = self._get_site_uid()
        if not uid:
            return None, None, ""
        try:
            session = self._session()
            url = f"{self._get_site_url()}/rescuesettleinfo.php?id={uid}"
            r = session.get(url, timeout=20)
            r.raise_for_status()
            soup = BeautifulSoup(r.text, "html.parser")
            table = None
            for tb in soup.find_all("table"):
                if "获得的憨豆" in tb.get_text() and "结算时间" in tb.get_text():
                    table = tb
                    break
            if not table:
                return None, None, ""
            rows = table.select("tr")
            best = None
            best_date = ""
            for tr in rows:
                tds = tr.find_all(["td", "th"])
                if len(tds) < 8:
                    continue
                cells = [c.get_text(strip=True) for c in tds]
                try:
                    bean = float(re.sub(r'[^\d.]', '', cells[4]) or 0)
                    pt = float(re.sub(r'[^\d.]', '', cells[5]) or 0)
                    dstr = cells[7]
                    dmatch = re.search(r'\d{4}-\d{2}-\d{2}', dstr)
                    if not dmatch:
                        continue
                    d = dmatch.group(0)
                    if d >= best_date:
                        best_date = d
                        best = (bean, pt)
                except (ValueError, IndexError):
                    continue
            if best:
                return best[0], best[1], best_date
        except Exception as e:
            logger.warning(f"抓取上次结算失败：{e}")
        return None, None, ""

    def _fetch_rescue_all(self, logs: list) -> List[dict]:
        seeds = []
        session = self._session()
        max_page = 1
        site_url = self._get_site_url()

        def _try_get(u):
            r = session.get(u, timeout=30)
            r.raise_for_status()
            return r.text

        try:
            html = _try_get(f"{site_url}/rescue.php?page=0")
            page_seeds, max_page = self._parse_rescue_page(html)
            seeds.extend(page_seeds)
            logs.append(f"保种区第1页获取 {len(page_seeds)} 条数据")
        except Exception as e:
            alt = site_url.replace("hhanclub.net", "hhancclub.net") if "hhanclub.net" in site_url \
                else site_url.replace("hhancclub.net", "hhanclub.net")
            if alt != site_url:
                try:
                    html = _try_get(f"{alt}/rescue.php?page=0")
                    site_url = alt
                    page_seeds, max_page = self._parse_rescue_page(html)
                    seeds.extend(page_seeds)
                    logs.append(f"保种区第1页已通过备用域名访问：{alt}")
                except Exception as e2:
                    logs.append(f"保种区第1页获取失败（含备用域名）：{e2}")
                    return []
            else:
                logs.append(f"保种区第1页获取失败：{e}")
                return []
        for page in range(1, max_page + 1):
            try:
                r = session.get(f"{site_url}/rescue.php?page={page}", timeout=30)
                r.raise_for_status()
                page_seeds, _ = self._parse_rescue_page(r.text)
                seeds.extend(page_seeds)
                logs.append(f"保种区第{page + 1}页获取 {len(page_seeds)} 条数据")
            except Exception as e:
                logs.append(f"保种区第{page + 1}页获取失败：{e}")
                break
            time.sleep(0.5)
        return seeds

    def _parse_rescue_page(self, html: str) -> Tuple[List[dict], int]:
        seeds = []
        max_page = 1
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.select('a[href*="page="]'):
            m = re.search(r'[?&]page=(\d+)', a.get("href", ""))
            if m:
                max_page = max(max_page, int(m.group(1)))
        for row in soup.select("div.torrent-table-sub-info"):
            s = self._parse_seed_row(row)
            if s:
                seeds.append(s)
        return seeds, max_page

    def _parse_seed_row(self, row) -> Optional[dict]:
        a_title = row.select_one("a.torrent-info-text-name")
        title = a_title.get_text(strip=True) if a_title else ""
        if not title:
            for a in row.find_all("a"):
                t = a.get_text(strip=True)
                if len(t) > 10:
                    title = t
                    break
        a_dl = row.select_one('a[href*="download.php"]')
        href = a_dl.get("href", "") if a_dl else ""
        size = None
        nums = []
        stats_el = row.select_one(".w-\\[20\\%\\]")
        leaves = []
        if stats_el:
            for el in stats_el.find_all(True):
                if el.find(True) is None and el.get_text(strip=True):
                    leaves.append(el.get_text(strip=True))
        for leaf in leaves:
            if size is None and re.search(r'(TB|GB|MB|KB)', leaf, re.I):
                size = size_to_gb(leaf)
                continue
            if re.match(r'^\d+(\.\d+)?$', leaf):
                nums.append(float(leaf))
        if size is None:
            for el in row.find_all(True):
                txt = el.get_text(strip=True)
                if el.find(True) is None and re.search(r'^\s*[\d.]+\s*(TB|GB|MB|KB)\s*$', txt, re.I):
                    size = size_to_gb(txt)
                    break
        if size is None:
            return None
        seeders = int(nums[0]) if nums else 0
        now_seeders = int(nums[1]) if len(nums) > 1 else 0
        if tier_of(seeders) >= 99:
            return None
        seed_id = None
        m_id = re.search(r'details\.php\?id=(\d+)', str(a_title.get("href") if a_title else ""))
        if m_id:
            seed_id = int(m_id.group(1))
        return {
            "title": title,
            "href": href,
            "size": size,
            "seeders": seeders,
            "now_seeders": now_seeders,
            "seed_id": seed_id,
        }

    def _get_current_seeding(self, logs: list) -> dict:
        result = {"count": 0, "total_gb": 0.0, "seeds": []}
        completed = self._fetch_completed(logs)
        if not completed:
            logs.append("完成页未获取到数据，本次按空保种降级处理")
            result["degraded"] = "完成页未获取到数据"
            return result
        dl_seeds = self._get_downloader_seeds(logs)
        if dl_seeds is None:
            logs.append("下载器未获取到种子，按站点完成页全量显示")
            result["count"] = len(completed)
            result["total_gb"] = sum(x.get("size", 0.0) for x in completed)
            result["seeds"] = completed
            return result
        dl_names = set(dl_seeds)
        norm_map = {}
        for x in completed:
            norm_map.setdefault(HHClubButler._norm_title(x["title"]), x)
        matched_map = {}
        for dn in dl_names:
            nd = HHClubButler._norm_title(dn)
            hit = None
            if nd in norm_map:
                hit = norm_map[nd]
            elif len(nd) >= 15:
                for nd_key, x in norm_map.items():
                    if len(nd_key) >= 15 and (nd in nd_key or nd_key in nd):
                        hit = x
                        break
            if hit is not None:
                matched_map.setdefault(hit["title"], hit)
        matched = list(matched_map.values())
        result["count"] = len(matched)
        result["total_gb"] = sum(x["size"] for x in matched)
        result["seeds"] = matched
        logs.append(f"完成页 {len(completed)} 个 ∩ 下载器做种 {len(dl_names)} 个 = 当前保种 {len(matched)} 个")
        return result

    def _build_overview_from_current(self, cur: dict) -> dict:
        seeds = cur.get("seeds", [])
        dist = {"0-1人": {"count": 0, "gb": 0.0},
                "2-3人": {"count": 0, "gb": 0.0},
                "4-5人": {"count": 0, "gb": 0.0}}
        for s in seeds:
            t = tier_of(s.get("seeders", 0))
            if t > 2:
                t = 2
            dist[TIER_NAMES[t]]["count"] += 1
            dist[TIER_NAMES[t]]["gb"] += s.get("size", 0.0)
        total_gb = cur.get("total_gb", 0.0)
        last_bean, last_pt, last_date = self._fetch_last_settlement()
        return {
            "count": cur.get("count", 0),
            "total_gb": total_gb,
            "total_tb": total_gb / 1024.0,
            "last_bean": last_bean,
            "last_pt": last_pt,
            "last_date": last_date,
            "dist": dist,
            "ok": not cur.get("error") and not cur.get("degraded"),
        }

    def _fetch_completed(self, logs: list) -> List[dict]:
        uid = self._get_site_uid()
        if not uid:
            logs.append("未获取到站点UID")
            return []
        items = []
        session = self._session()
        url = f"{self._get_site_url()}/userdetails.php?id={uid}&action=7"
        max_page = 0
        try:
            r = session.get(url, timeout=30)
            r.raise_for_status()
            items, max_page = self._parse_completed_page(r.text)
        except Exception as e:
            alt = url.replace("hhanclub.net", "hhancclub.net") if "hhanclub.net" in url \
                else url.replace("hhancclub.net", "hhanclub.net")
            if alt != url:
                try:
                    r = session.get(alt, timeout=30)
                    r.raise_for_status()
                    url = alt
                    items, max_page = self._parse_completed_page(r.text)
                    logs.append(f"完成页已通过备用域名访问：{alt}")
                except Exception as e2:
                    logs.append(f"完成页第1页获取失败（含备用域名）：{e2}")
                    return []
            else:
                logs.append(f"完成页第1页获取失败：{e}")
                return []
        for page in range(1, max_page + 1):
            try:
                r = session.get(f"{url}&page={page}", timeout=30)
                r.raise_for_status()
                page_items, _ = self._parse_completed_page(r.text)
                items.extend(page_items)
            except Exception as e:
                logs.append(f"完成页第{page + 1}页获取失败：{e}")
                break
            time.sleep(0.5)
        return items

    @staticmethod
    def _norm_title(s) -> str:
        if not s:
            return ""
        s = str(s).lower()
        return re.sub(r'[^0-9a-z\u4e00-\u9fff]', '', s)

    def _parse_completed_page(self, html: str) -> Tuple[List[dict], int]:
        items = []
        max_page = 0
        soup = BeautifulSoup(html, "html.parser")
        m = re.search(r"var\s+maxpage\s*=\s*(\d+)", html)
        if m:
            max_page = int(m.group(1))
        table = None
        for tb in soup.find_all("table"):
            if "种子ID" in tb.get_text() and tb.select("tr.text-center"):
                table = tb
                break
        if not table:
            return items, max_page
        header_tr = table.find("tr")
        headers = [th.get_text(strip=True) for th in header_tr.find_all(["th", "td"])] if header_tr else []
        idx_size = 2
        idx_n = 3
        for i, h in enumerate(headers):
            if "大小" in h:
                idx_size = i
            if "初始保种" in h:
                idx_n = i
        for tr in table.select("tr.text-center"):
            tds = tr.find_all("td")
            if len(tds) <= max(idx_size, idx_n):
                continue
            a = tds[idx_n - 1].find("a") if len(tds) > 1 else None
            if a is None:
                a = tds[1].find("a")
            title = a.get_text(strip=True) if a else tds[1].get_text(strip=True)
            size = size_to_gb(tds[idx_size].get_text(strip=True))
            n_str = tds[idx_n].get_text(strip=True)
            try:
                n = int(n_str)
            except ValueError:
                n = 1
            if not size or tier_of(n) >= 99:
                continue
            seed_id = None
            try:
                sid_txt = tds[0].get_text(strip=True)
                seed_id = int(sid_txt) if sid_txt.isdigit() else None
            except Exception:
                pass
            # column indices: 0=ID,1=name,2=size,3=init,4=now,5=完成时间,6=上次结算,7=今日做种,8=今日达标
            now_seeders = 0
            try:
                now_seeders = int(tds[4].get_text(strip=True))
            except Exception:
                pass
            completed_at = tds[5].get_text(strip=True) if len(tds) > 5 else ""
            last_settle = tds[6].get_text(strip=True) if len(tds) > 6 else ""
            today_hours = tds[7].get_text(strip=True) if len(tds) > 7 else ""
            qualified = tds[8].get_text(strip=True) if len(tds) > 8 else ""
            items.append({
                "title": title, "size": size, "seeders": n, "seed_id": seed_id,
                "now_seeders": now_seeders,
                "completed_at": completed_at,
                "last_settle": last_settle,
                "today_hours": today_hours,
                "qualified": qualified,
            })
        return items, max_page

    def _get_downloader_obj(self):
        if not self._downloader:
            return None
        try:
            services = DownloaderHelper().get_services(name_filters=[self._downloader])
            if not services:
                return None
            service = services.get(self._downloader)
            if not service or not service.instance:
                return None
            if service.instance.is_inactive():
                return None
            return service
        except Exception:
            return None

    @staticmethod
    def _extract_tracker_text(t, dl_type: str = "") -> str:
        try:
            parts = []
            if isinstance(t, dict):
                parts.append(str(t.get("tracker") or ""))
                tr = t.get("trackers")
                if isinstance(tr, (list, tuple)):
                    parts.extend(str(x) for x in tr)
                parts.append(str(t.get("trackerList") or ""))
                stats = t.get("trackerStats") or []
                if isinstance(stats, list):
                    for s in stats:
                        if isinstance(s, dict):
                            parts.append(str(s.get("announce") or ""))
                parts.append(str(t.get("magnet_uri") or ""))
            else:
                parts.append(str(getattr(t, "tracker", "") or ""))
                tr = getattr(t, "trackers", None)
                if isinstance(tr, (list, tuple)):
                    parts.extend(str(x) for x in tr)
                parts.append(str(getattr(t, "trackerList", "") or ""))
                stats = getattr(t, "trackerStats", None) or []
                if isinstance(stats, list):
                    for s in stats:
                        try:
                            announce = s.get("announce") if isinstance(s, dict) else getattr(s, "announce", None)
                            if announce:
                                parts.append(str(announce))
                        except Exception:
                            pass
                try:
                    mg = getattr(t, "magnet_uri", None) or getattr(t, "magnet", None)
                    if callable(mg):
                        mg = mg()
                    if mg:
                        parts.append(str(mg))
                except Exception:
                    pass
            return " ".join(p for p in parts if p)
        except Exception:
            return ""

    @staticmethod
    def _get_progress_ratio(t, dl_type: str = "") -> Optional[float]:
        try:
            if isinstance(t, dict):
                progress = t.get("progress")
                if progress is None:
                    progress = t.get("percent_done")
            else:
                progress = getattr(t, "progress", None)
                if progress is None:
                    progress = getattr(t, "percent_done", None)
            if progress is None:
                return None
            progress = float(progress)
            dl = (dl_type or "").lower()
            if ("transmission" in dl or "tr_" in dl) and progress > 1.0:
                progress /= 100.0
            return max(0.0, min(1.0, progress))
        except Exception:
            return None

    def _get_downloader_seeds(self, logs: list, only_completed: bool = True,
                                any_tracker: bool = False) -> Optional[set]:
        service = self._get_downloader_obj()
        if not service:
            logs.append("未配置有效的下载器")
            return None
        dl_type = ""
        try:
            dl_type = str(service.type or service.config.type or "")
        except Exception:
            pass
        try:
            torrents, error = service.instance.get_torrents()
            if error:
                logs.append("获取下载器种子列表出错")
                return None
            names = set()
            site_total = 0
            for t in torrents:
                tracker = ""
                try:
                    tracker = HHClubButler._extract_tracker_text(t, dl_type)
                except Exception:
                    pass
                if any_tracker or ("hhanclub" in tracker or "hhclub" in tracker
                                   or "hanclub" in tracker):
                    site_total += 1
                if only_completed:
                    progress = HHClubButler._get_progress_ratio(t, dl_type)
                    if progress is not None and progress < 1.0:
                        continue
                try:
                    name = t.get("name") if isinstance(t, dict) else getattr(t, "name", "")
                except Exception:
                    name = ""
                if name:
                    names.add(name)
            if only_completed:
                logs.append(f"下载器 {len(torrents)} 个种子，本站tracker {site_total} 个，已完成做种 {len(names)} 个")
            return names
        except Exception as e:
            logs.append(f"获取下载器种子失败：{e}")
            return None

    def _push_seeds(self, seeds: list, logs: list):
        service = self._get_downloader_obj()
        if not service:
            logs.append("未配置有效的下载器，无法推送")
            return 0, 0, []
        session = self._session()
        push_cookie = self._get_site_cookie() or None
        ok_count = 0
        fail_list = []
        for s in seeds:
            href = s.get("href", "")
            title = s.get("title", "")
            if not href:
                fail_list.append({"title": title, "reason": "缺少下载链接"})
                continue
            url = href if href.startswith("http") else f"{self._get_site_url()}/{href.lstrip('/')}"
            try:
                r = session.get(url, timeout=60)
                if r.status_code == 200 and r.content:
                    dl_type = ""
                    try:
                        dl_type = service.config.type or ""
                    except Exception:
                        pass
                    kwargs = {}
                    if self._tag:
                        dl_l = str(dl_type).lower()
                        if "qbittorrent" in dl_l:
                            kwargs["tag"] = self._tag
                        elif "transmission" in dl_l:
                            kwargs["labels"] = [self._tag]
                        else:
                            kwargs["tag"] = self._tag
                    success = service.instance.add_torrent(
                        content=r.content, download_dir=self._save_path or None,
                        cookie=push_cookie, **kwargs)
                    if success:
                        ok_count += 1
                        logs.append(f"✅ 已添加：{title}")
                    else:
                        fail_list.append({"title": title, "reason": "下载器拒绝添加"})
                else:
                    fail_list.append({"title": title, "reason": f"种子文件下载失败(HTTP {r.status_code})"})
            except Exception as e:
                fail_list.append({"title": title, "reason": f"推送异常：{e}"})
            time.sleep(1)
        return ok_count, len(seeds) - ok_count, fail_list

    def _delete_seeds(self, seeds: list, logs: list):
        service = self._get_downloader_obj()
        if not service:
            logs.append("未配置有效的下载器，无法删除")
            return
        try:
            torrents, error = service.instance.get_torrents()
            if error:
                return
            del_norms = set()
            for s in seeds:
                n = HHClubButler._norm_title(s.get("title") or "")
                if n:
                    del_norms.add(n)
            if not del_norms:
                return
            del_ids = []
            for t in torrents:
                try:
                    tname = t.get("name") if isinstance(t, dict) else getattr(t, "name", "")
                    thash = (t.get("hash") if isinstance(t, dict)
                             else getattr(t, "hash", None) or getattr(t, "hashString", ""))
                except Exception:
                    continue
                if HHClubButler._norm_match(tname, del_norms):
                    del_ids.append(thash)
            if del_ids:
                service.instance.delete_torrents(delete_file=True, ids=del_ids)
                logs.append(f"已删除 {len(del_ids)} 个低档位种子（任务+文件）")
        except Exception as e:
            logs.append(f"删除种子失败：{e}")

    @staticmethod
    def _norm_match(name: str, targets: set) -> bool:
        n = HHClubButler._norm_title(name)
        if not n:
            return False
        if n in targets:
            return True
        if len(n) >= 15:
            for t in targets:
                if len(t) >= 15 and (n in t or t in n):
                    return True
        return False

    def _clean_stale_downloads(self, logs: list, seeds: list, extra_titles: set = None):
        service = self._get_downloader_obj()
        if not service:
            return 0
        dl_type = ""
        try:
            dl_type = str(service.type or service.config.type or "")
        except Exception:
            pass
        try:
            torrents, error = service.instance.get_torrents()
            if error:
                return 0
        except Exception:
            return 0
        try:
            site_titles = {HHClubButler._norm_title(s.get("title") or "") for s in seeds}
            if extra_titles:
                site_titles |= set(extra_titles)
        except Exception:
            site_titles = set(extra_titles or [])
        now = time.time()
        threshold = self._auto_clean_days * 86400
        stale = []
        for t in torrents:
            try:
                tracker = HHClubButler._extract_tracker_text(t)
                if isinstance(t, dict):
                    name = str(t.get("name") or "")
                    progress = t.get("progress")
                    added = (t.get("added_on") or t.get("added_time") or t.get("added_date") or 0)
                    tid = t.get("hash") or t.get("id")
                else:
                    name = str(getattr(t, "name", "") or "")
                    progress = HHClubButler._get_progress_ratio(t, dl_type)
                    added = (getattr(t, "added_on", 0) or getattr(t, "added_time", 0)
                             or getattr(t, "added_date", 0) or 0)
                    tid = getattr(t, "hash", None) or getattr(t, "hashString", None) or getattr(t, "id", None)
            except Exception:
                continue
            is_site = ("hhanclub" in tracker or "hhclub" in tracker or "hanclub" in tracker)
            if not is_site and site_titles:
                n = HHClubButler._norm_title(name)
                if n and n in site_titles:
                    is_site = True
            if not is_site:
                continue
            try:
                progress = float(progress) if progress is not None else 1.0
            except (TypeError, ValueError):
                progress = 1.0
            if progress > 1.0:
                progress /= 100.0
            if progress >= 1.0:
                continue
            try:
                if hasattr(added, "timestamp"):
                    added = added.timestamp()
                added = float(added)
            except (TypeError, ValueError, OSError, OverflowError):
                added = 0.0
            if added <= 0:
                continue
            if now - added >= threshold:
                stale.append((tid, name))
        if not stale:
            logs.append(f"未完成清理：无超过 {self._auto_clean_days:g} 天未完成的任务")
            return 0
        ids = [tid for tid, _ in stale if tid]
        if not ids:
            return 0
        try:
            service.instance.delete_torrents(delete_file=True, ids=ids)
            logs.append(f"未完成清理：已删除 {len(ids)} 个超时任务（含未完成文件）")
            return len(ids)
        except Exception as e:
            logs.append(f"未完成清理失败：{e}")
            return 0

    @staticmethod
    def _parse_seeder_range(raw) -> Optional[tuple]:
        if raw is None:
            return None
        s = str(raw).strip()
        if "-" in s:
            parts = s.split("-", 1)
            try:
                lo = int(parts[0].strip())
                hi = int(parts[1].strip())
                return (min(lo, hi), max(lo, hi))
            except (ValueError, IndexError):
                return None
        try:
            n = int(s)
            return (n, n)
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _tier_sort_key(s: dict):
        return (tier_of(s.get("seeders", 1)), -s.get("size", 0.0))

    def _optimize_incremental(self, seeds: list, eff_target: float,
                              current_gb: float, logs: list) -> dict:
        cap = eff_target
        if cap <= 0:
            logs.append("当前保种体积已达目标，无需新增")
            return {"picked": [], "total_gb": 0.0, "del_seeds": []}
        candidates = sorted(seeds, key=HHClubButler._tier_sort_key)
        picked = []
        used = 0.0
        for s in candidates:
            if used + s["size"] <= cap + 1e-6:
                picked.append(s)
                used += s["size"]
        logs.append(f"增量优选：剩余可增 {cap:.1f} GB，按档位高→低补 {len(picked)} 个，新增 {used:.1f} GB")
        for s in picked[:8]:
            logs.append(f"  + {s.get('title','')} | {s.get('size',0.0):.1f} GB | "
                        f"初始做种 {s.get('seeders','?')} 人")
        if len(picked) > 8:
            logs.append(f"  …等共 {len(picked)} 个")
        return {"picked": picked, "total_gb": used, "del_seeds": []}

    def _optimize_wash(self, candidates: list, current: list,
                       eff_target: float, logs: list) -> dict:
        cap = self._target_volume
        if not cap or cap <= 0:
            logs.append("目标体积为0（不限），换种模式不删除任何种子")
            return {"picked": [], "del_seeds": [], "total_gb": 0.0, "keep_count": len(current)}
        cur_titles = {s["title"] for s in current}
        # 从当前在保种子开始，默认全部保留
        final = list(current)
        total_gb = sum(s.get("size", 0.0) for s in final)
        # 新候选按档位优先级排序（高档位先处理，同档位大体积先）
        new_cands = [s for s in candidates if s["title"] not in cur_titles]
        new_cands.sort(key=lambda s: (tier_of(s.get("seeders", 1)), -s.get("size", 0.0)))
        del_map = {}
        for cand in new_cands:
            csize = cand.get("size", 0.0)
            if csize > cap:
                continue
            # 能直接放下就加
            if total_gb + csize <= cap + 1e-6:
                final.append(cand)
                total_gb += csize
                continue
            # 放不下：只踢比候选档位更低的种子（低档优先、小体积优先）
            cand_tier = tier_of(cand.get("seeders", 1))
            victims = [s for s in final
                       if tier_of(s.get("seeders", 1)) > cand_tier
                       and s["title"] not in del_map]
            victims.sort(key=lambda s: s.get("size", 0.0))
            evicted_this_round = []
            added = False
            for victim in victims:
                final.remove(victim)
                evicted_this_round.append(victim)
                total_gb -= victim["size"]
                if total_gb + csize <= cap + 1e-6:
                    final.append(cand)
                    total_gb += csize
                    for v in evicted_this_round:
                        del_map[v["title"]] = v
                    added = True
                    break
            if not added:
                # 回滚本次尝试踢掉的种子
                for v in evicted_this_round:
                    final.append(v)
                    total_gb += v.get("size", 0.0)
        del_seeds = list(del_map.values())
        add_seeds = [s for s in final if s["title"] not in cur_titles]
        keep_count = len(final) - len(add_seeds)
        logs.append(f"换种优选（体积上限 {cap:g} GB）：构建 {len(final)} 个"
                    f"（保留当前 {keep_count} + 新增 {len(add_seeds)}），"
                    f"删除低档位 {len(del_seeds)} 个，最终体积 {total_gb:.1f} GB")
        for s in add_seeds[:8]:
            logs.append(f"  + 新增: {s.get('title','')} | {s.get('size',0.0):.1f} GB | "
                        f"初始做种 {s.get('seeders','?')} 人")
        if len(add_seeds) > 8:
            logs.append(f"  …等共 {len(add_seeds)} 个新增")
        for s in del_seeds[:8]:
            logs.append(f"  - 删除: {s.get('title','')} | {s.get('size',0.0):.1f} GB | "
                        f"初始做种 {s.get('seeders','?')} 人")
        if len(del_seeds) > 8:
            logs.append(f"  …等共 {len(del_seeds)} 个删除")
        return {"picked": add_seeds, "del_seeds": del_seeds,
                "total_gb": total_gb, "keep_count": keep_count}

    def _save_log(self, logs: list):
        try:
            path = self.get_data_path()
            if not path.exists():
                path.mkdir(parents=True, exist_ok=True)
            with open(path / "run_log.txt", "w", encoding="utf-8") as f:
                f.write("\n".join(logs))
        except Exception as e:
            logger.error(f"保存日志失败：{e}")
