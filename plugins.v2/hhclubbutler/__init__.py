# -*- coding: utf-8 -*-
"""
HHCLUB 憨憨保种区管家插件（MoviePilot V2）
=================================================
功能：
1. 定时/手动抓取憨憨保种区全部种子（rescue.php 全页）
2. 按目标积分（默认1800）用 DP 背包算法优选：占用最少保种体积达到目标每日积分
3. 支持按体积模式：保种体积固定时积分最大化
4. 支持择优换种：自动删除低效率已保种种子、下载高效率候选种子
5. 一键推送到 MoviePilot 已配置的 QB/TR 下载器，保存路径自定义
6. 数据页展示最近一次优选结果与今日保种概况

公式（经用户结算日志 + Excel 达标池 109 项交叉验证）：
    每日憨豆 = 0.018467 × 体积(GB) × 18h × 憨豆倍率
    每日积分 = 0.018195 × 体积(GB) × 18h × 积分倍率
    倍率（按初始保种人数）：憨豆 0-1人×3 / 2-3人×2 / 4-5人×1.5
                           积分 0-1人×2 / 2-3人×1.75 / 4-5人×1.5
    超过5人：无保种区奖励
"""

import re
import time
import threading
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.db.site_oper import SiteOper
from app.helper.downloader import DownloaderHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import NotificationType

import requests
from bs4 import BeautifulSoup

lock = threading.Lock()

# ============================================================
# 公式常量（用户结算日志+Excel校准，勿改）
# ============================================================
C_BEAN = 0.018467   # 憨豆基础系数：每GB每小时基础憨豆
C_PT = 0.018195     # 积分基础系数
HOURS = 18          # 保种区每日按18小时计

# 憨豆倍率表（按初始保种人数）
BEAN_MUL = {1: 3.0, 2: 2.0, 3: 2.0, 4: 1.5, 5: 1.5}
# 积分倍率表
PT_MUL = {1: 2.0, 2: 1.75, 3: 1.75, 4: 1.5, 5: 1.5}


def multiplier(n: Optional[int]) -> Tuple[float, float]:
    """按初始保种人数取倍率，返回(憨豆倍率, 积分倍率)；0人按1人档；>5人无奖励"""
    if not n or n <= 0:
        n = 1
    if n > 5:
        return 0.0, 0.0
    return BEAN_MUL.get(n, 1.5), PT_MUL.get(n, 1.5)


def size_to_gb(text: str) -> Optional[float]:
    """'44.23GB' / '1.5TB' / '800MB' -> GB"""
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


class HHClubButler(_PluginBase):
    # 插件名称
    plugin_name = "憨憨保种区管家"
    # 插件描述
    plugin_desc = "自动化优选添加及换种工具"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/SixOrg/MoviePilot-Plugins/main/plugins.v2/hhclubbutler/hhclubbutler.png"
    # 插件版本
    plugin_version = "0.16"
    # 插件作者
    plugin_author = "六个橙子"
    # 作者主页
    author_url = "https://github.com/SixOrg"
    # 插件配置项ID前缀
    plugin_config_prefix = "hhclubbutler_"
    # 加载顺序
    plugin_order = 20
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _enabled: bool = False
    _onlyonce: bool = False
    _notify: bool = False
    _cron: str = "30 14 * * *"
    _cookie: str = ""
    _site_url: str = ""
    _uid: str = ""
    _mode: str = "incremental"
    _use_volume: bool = False
    _target_pt: float = 1800
    _target_volume: float = 0
    _seeder_cond: str = ""
    _exclude_zero: bool = False
    _downloader: str = ""
    _save_path: str = ""
    _tag: str = ""
    _auto_clean_days: float = 0.0
    _scheduler = None

    # 运行状态
    _running: bool = False
    _last_result: str = "尚未运行"
    _last_overview: dict = None

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
        # 兼容旧配置：v1.0.27 起已移除“基础订阅”模式，自动回落到增量优选
        if self._mode == "subscribe":
            self._mode = "incremental"
        _uv = config.get("use_volume")
        if isinstance(_uv, str):
            self._use_volume = _uv.strip().lower() in ("1", "true", "yes", "on")
        else:
            self._use_volume = bool(_uv)
        try:
            raw_pt = float(config.get("target_pt") or 1800)
        except (TypeError, ValueError):
            raw_pt = 1800
        # 站点积分上限1800硬性锁定：超过自动按1800，并回写配置纠正（输入框同步显示1800）
        self._target_pt = min(1800.0, raw_pt)
        if raw_pt > 1800:
            try:
                self._update_cfg(target_pt=1800)
            except Exception:
                pass
        try:
            self._target_volume = float(config.get("target_volume") or 2000)
        except (TypeError, ValueError):
            self._target_volume = 2000
        # 做种人数条件：增量优选过滤用；兼容旧版“基础订阅”的 subscribe_n 配置
        raw_cond = config.get("seeder_cond")
        if raw_cond is None or str(raw_cond).strip() == "":
            if config.get("subscribe_n") is not None:
                raw_cond = str(config.get("subscribe_n"))
        self._seeder_cond = str(raw_cond or "").strip()
        self._exclude_zero = bool(config.get("exclude_zero"))
        self._downloader = config.get("downloader") or ""
        self._save_path = config.get("save_path") or ""
        self._tag = config.get("tag") or ""
        try:
            self._auto_clean_days = float(config.get("auto_clean_days") or 0)
        except (TypeError, ValueError):
            self._auto_clean_days = 0
        self._auto_clean_days = max(0.0, min(365.0, self._auto_clean_days))

        # 配置生效日志：置于全部配置读取之后，按当前生效模式只显示对应目标值
        # （积分模式不显示体积目标，体积模式不显示积分目标，避免混显误会）
        if self._use_volume:
            logger.info(f"憨憨保种区管家配置生效：启用={self._enabled} 通知={self._notify} "
                        f"模式={self._mode} 目标体积={self._target_volume}")
        else:
            logger.info(f"憨憨保种区管家配置生效：启用={self._enabled} 通知={self._notify} "
                        f"模式={self._mode} 目标积分={self._target_pt}")

        if self._onlyonce:
            self._onlyonce = False
            self.update_config({
                "enabled": self._enabled,
                "onlyonce": False,
                "notify": self._notify,
                "cron": self._cron,
                "mode": self._mode,
                "use_volume": self._use_volume,
                "target_pt": self._target_pt,
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
            # 立即运行一次
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
            "path": "/run",
            "endpoint": self.api_run,
            "methods": ["POST"],
            "auth": "apikey",
            "summary": "立即运行优选",
            "description": "抓取保种区并优选推送",
        }, {
            "path": "/result",
            "endpoint": self.api_result,
            "methods": ["GET"],
            "auth": "apikey",
            "summary": "最近运行结果",
            "description": "查看最近一次优选结果",
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
                    {
                        # 顶部保种区概况卡片（纯Vuetify组件，数据=最近一次运行缓存，打开设置不实时抓取）
                        **self._overview_form_cards()
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [
                                    {'component': 'VSwitch', 'props': {'model': 'enabled', 'label': '启用插件'}}
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [
                                    {'component': 'VSwitch', 'props': {'model': 'onlyonce', 'label': '立即运行一次'}}
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [
                                    {'component': 'VSwitch', 'props': {'model': 'notify', 'label': '发送通知'}}
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 6},
                                'content': [
                                    {
                                        'component': 'VCronField',
                                        'props': {
                                            'model': 'cron',
                                            'label': '执行周期',
                                            'placeholder': '30 14 * * *（每天14:30，保种区14:10更新后）'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 6},
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'model': 'downloader',
                                            'label': '下载器',
                                            'items': downloaders
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 6},
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'save_path',
                                            'label': '保存路径',
                                            'placeholder': '如 /downloads/下载/保种'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 6},
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'tag',
                                            'label': '自定义标签',
                                            'placeholder': '如 hhan（推送到下载器后自动打标）'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 6},
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'model': 'mode',
                                            'label': '优选模式',
                                            'items': [
                                                {'title': '增量优选（不删保种&补齐达标）', 'value': 'incremental'},
                                                {'title': '换种优选（删除低效&下载高效）', 'value': 'wash'}
                                            ]
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 6},
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'exclude_zero',
                                            'label': '排除0做种人数种子',
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    # 目标设置区：全部字段常驻，前端按 model 动态显隐（show 表达式）
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 6},
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'model': 'use_volume',
                                            'label': '目标类型',
                                            'items': [
                                                {'title': '按目标积分', 'value': False},
                                                {'title': '按目标体积', 'value': True}
                                            ]
                                        }
                                    },
                                    {
                                        'component': 'VRow',
                                        'props': {'no-gutters': True},
                                        'content': [
                                            {
                                                'component': 'VCol',
                                                'props': {
                                                    'cols': 12,
                                                    'show': '{{mode == "wash" && !use_volume}}'
                                                },
                                                'content': [
                                                    {
                                                        'component': 'div',
                                                        'props': {
                                                            'style': 'color:#e53935;font-size:12px;margin-top:4px;line-height:1.4;padding-left:16px;'
                                                        },
                                                        'text': '※若目标值低于实际积分值，将自动删除超标文件※'
                                                    }
                                                ]
                                            },
                                            {
                                                'component': 'VCol',
                                                'props': {
                                                    'cols': 12,
                                                    'show': '{{mode == "wash" && use_volume}}'
                                                },
                                                'content': [
                                                    {
                                                        'component': 'div',
                                                        'props': {
                                                            'style': 'color:#e53935;font-size:12px;margin-top:4px;line-height:1.4;padding-left:16px;'
                                                        },
                                                        'text': '※若目标值低于实际体积值，将自动删除超标文件※'
                                                    }
                                                ]
                                            },
                                        ]
                                    },
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12, 'md': 6,
                                    'show': '{{!use_volume}}'
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'target_pt',
                                            'label': '目标积分（每日）',
                                            'type': 'number',
                                            'max': 1800,
                                            'hint': '含已做种总积分，上限1800',
                                            'persistent-hint': True
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12, 'md': 6,
                                    'show': '{{use_volume}}'
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'target_volume',
                                            'label': '目标体积（GB，总保种上限）',
                                            'type': 'number',
                                            'hint': '含已做种总体积，0=不限',
                                            'persistent-hint': True
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 6},
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'seeder_cond',
                                            'label': '做种人数',
                                            'hint': '支持输入单值或区间，例如：1或1-5，留空=系统自行优选',
                                            'persistent-hint': True
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 6},
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'auto_clean_days',
                                            'label': '自动清理未完成下载（天）',
                                            'hint': '超过N天仍未下载完成自动删除（含未完成文件）；留空或0=不清理',
                                            'persistent-hint': True
                                        }
                                    }
                                ]
                            },
                        ]
                    },
                ]
            }
        ]
        defaults = {
            "enabled": False,
            "onlyonce": False,
            "notify": True,
            "cron": "30 14 * * *",
            "mode": "incremental",
            "use_volume": False,
            "target_pt": 1800,
            "target_volume": 0,
            "seeder_cond": "",
            "exclude_zero": False,
            "downloader": "",
            "save_path": "",
            "tag": "",
            "auto_clean_days": 0
        }
        return form, defaults

    def _overview_form_cards(self) -> dict:
        """设置页顶部概况卡片：纯Vuetify组件拼装（兼容不支持html字段的表单渲染器，任何MP版本可渲染）"""
        if not self._last_overview or not self._last_overview.get("ok"):
            return {
                'component': 'VCard',
                'props': {'flat': True},
                'content': [
                    {
                        'component': 'VCardText',
                        'props': {},
                        'text': '尚未运行或未获取到数据（运行一次后此处显示最近概况）'
                    }
                ]
            }
        ov = self._last_overview
        d = ov["dist"]
        c = ov.get("count", 0) or sum(x["count"] for x in d.values()) or 1
        c01 = d["0-1人"]["count"] / c * 100.0
        c23 = d["2-3人"]["count"] / c * 100.0
        c45 = d["4-5人"]["count"] / c * 100.0

        def stat_col(label, num, sub=""):
            return {
                'component': 'VCol',
                'props': {'cols': 6, 'md': 3},
                'content': [
                    {'component': 'div',
                     'props': {'style': 'text-align:center;font-size:11px;color:rgba(var(--v-theme-on-surface),.6);white-space:nowrap;'},
                     'text': label},
                    {'component': 'div',
                     'props': {'style': 'text-align:center;font-size:17px;font-weight:700;white-space:nowrap;'},
                     'text': f'{num} {sub}'},
                ]
            }

        def legend_col(key, align):
            return {
                'component': 'VCol',
                'props': {'cols': 12, 'md': 4},
                'content': [
                    {'component': 'div',
                     'props': {'style': f'font-size:11px;color:rgba(var(--v-theme-on-surface),.6);white-space:nowrap;text-align:{align};'},
                     'text': f'{key} {d[key]["count"]}个 {d[key]["gb"]:.1f}GB'}
                ]
            }

        return {
            'component': 'VCard',
            'props': {'flat': True},
            'content': [
                {
                    'component': 'VCardText',
                    'props': {'class': 'pt-2'},
                    'content': [
                        {'component': 'VRow', 'content': [
                            stat_col("🌱 在保种子", ov.get("count", 0), "个"),
                            stat_col("💾 总体积", f'{ov.get("total_gb", 0.0):.1f}', "GB"),
                            stat_col("🥜 达标可得憨豆", f'+{ov.get("total_bean", 0.0):.1f}', ""),
                            stat_col("⭐ 达标可得积分", f'+{ov.get("total_pt", 0.0):.1f}', ""),
                        ]},
                        {'component': 'div',
                         'props': {'style': 'margin-top:8px;margin-bottom:4px;font-size:11px;color:rgba(var(--v-theme-on-surface),.6);'},
                         'text': '初始做种人数分布'},
                        # 堆积条：纯div+style（与四列数字同一渲染机制，最稳）
                        {'component': 'div',
                         'props': {'style': 'width:100%;display:flex;border-radius:4px;overflow:hidden;'},
                         'content': [
                             {'component': 'div',
                              'props': {'style': f'height:8px;width:{c01:.2f}%;background:#22c55e;'},
                              'text': ''},
                             {'component': 'div',
                              'props': {'style': f'height:8px;width:{c23:.2f}%;background:#3b82f6;'},
                              'text': ''},
                             {'component': 'div',
                              'props': {'style': f'height:8px;width:{c45:.2f}%;background:#ef4444;'},
                              'text': ''},
                         ]},
                        {'component': 'VRow', 'props': {'no-gutters': True}, 'content': [
                            legend_col("0-1人", "left"),
                            legend_col("2-3人", "center"),
                            legend_col("4-5人", "right"),
                        ]},
                    ]
                },
                {
                    'component': 'VAlert',
                    'props': {'type': 'info', 'variant': 'tonal',
                              'text': f"最近运行状态：{self._last_result}"}
                }
            ]
        }

    @staticmethod
    def _overview_html(ov: dict) -> str:
        """憨憨保种区管家概况（单行4列·透明背景·档位配色堆积条·达标可得口径）"""
        if not ov or ov.get("count", 0) <= 0:
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

        icons = {
            "在保种子": "🌱",
            "总体积": "💾",
            "达标可得憨豆": "🥜",
            "达标可得积分": "⭐",
        }

        def td(label, num, sub):
            return (
                f'<td style="text-align:center;padding:4px 2px;width:25%;vertical-align:top;">'
                f'<div style="font-size:10.5px;color:{ON60};white-space:nowrap;">'
                f'{icons[label]} {label}</div>'
                f'<div style="font-size:17px;font-weight:700;color:{ON};line-height:1.4;white-space:nowrap;">{num}'
                f'<span style="font-size:10px;font-weight:400;color:{ON40};"> {sub}</span></div></td>'
            )

        cells = (
            '<table style="width:100%;border-collapse:collapse;table-layout:fixed;margin:2px 0 6px;"><tr>'
            + td("在保种子", f'{ov["count"]}', "个")
            + td("总体积", f'{ov["total_gb"]:.1f}', "GB")
            + td("达标可得憨豆", f'+{ov["total_bean"]:.1f}', "")
            + td("达标可得积分", f'+{ov["total_pt"]:.1f}', "")
            + '</tr></table>'
        )
        bar = (
            '<table style="width:100%;border-collapse:collapse;table-layout:fixed;margin:6px 0 6px;"><tr>'
            f'<td style="height:10px;width:{pct("0-1人"):.2f}%;background:{C["0-1人"]};border-radius:5px 0 0 5px;"></td>'
            f'<td style="height:10px;width:{pct("2-3人"):.2f}%;background:{C["2-3人"]};"></td>'
            f'<td style="height:10px;width:{pct("4-5人"):.2f}%;background:{C["4-5人"]};border-radius:0 5px 5px 0;"></td>'
            '</tr></table>'
        )
        legend = (
            '<table style="width:100%;border-collapse:collapse;table-layout:fixed;font-size:10.5px;color:{ON60};"><tr>'
            f'<td style="text-align:left;white-space:nowrap;"><span style="display:inline-block;width:7px;height:7px;border-radius:2px;background:{C["0-1人"]};margin-right:3px;"></span>0-1人 {d["0-1人"]["count"]}个 {d["0-1人"]["gb"]:.1f}GB</td>'
            f'<td style="text-align:center;white-space:nowrap;"><span style="display:inline-block;width:7px;height:7px;border-radius:2px;background:{C["2-3人"]};margin-right:3px;"></span>2-3人 {d["2-3人"]["count"]}个 {d["2-3人"]["gb"]:.0f}GB</td>'
            f'<td style="text-align:right;white-space:nowrap;"><span style="display:inline-block;width:7px;height:7px;border-radius:2px;background:{C["4-5人"]};margin-right:3px;"></span>4-5人 {d["4-5人"]["count"]}个 {d["4-5人"]["gb"]:.1f}GB</td>'
            '</tr></table>'
        )
        dist_title = (
            '<table style="width:100%;border-collapse:collapse;table-layout:fixed;margin:2px 0 2px;"><tr>'
            f'<td style="text-align:left;font-size:10.5px;color:{ON60};">初始做种人数分布</td>'
            '</tr></table>'
        )
        return (
            f'<div style="background:transparent;">'
            f'{cells}{dist_title}{bar}{legend}</div>'
        )

    def get_page(self) -> List[dict]:
        """数据页：憨憨保种区管家概况卡片 + 最近运行状态"""
        overview = None
        try:
            overview = self._build_seeding_overview([])
        except Exception as e:
            logger.error(f"获取憨憨保种区管家概况失败：{e}")
        return [
            {
                'component': 'VCard',
                'props': {'flat': True},
                'content': [
                    {
                        'component': 'VCardText',
                        'props': {},
                        'content': [
                            {
                                'component': 'div',
                                'html': HHClubButler._overview_html(overview)
                            },
                            {
                                'component': 'VAlert',
                                'props': {'type': 'info', 'variant': 'tonal',
                                          'text': f"最近运行状态：{self._last_result}"}
                            }
                        ]
                    }
                ]
            }
        ]

    def get_dashboard_meta(self) -> Optional[List[Dict[str, str]]]:
        """仪表盘元信息"""
        return [{"key": "seeding", "name": "憨憨保种区管家"}]

    def get_dashboard(self, key: str, **kwargs) -> Optional[Tuple[Dict[str, Any], Dict[str, Any], Optional[List[dict]]]]:
        """仪表盘：今日保种概况卡片"""
        if key and key != "seeding":
            return None
        try:
            overview = self._build_seeding_overview([])
        except Exception as e:
            logger.error(f"仪表盘概况获取失败：{e}")
            overview = {"count": 0, "total_gb": 0.0, "total_tb": 0.0,
                        "total_bean": 0.0, "total_pt": 0.0,
                        "dist": {"0-1人": {"count": 0, "gb": 0.0},
                                 "2-3人": {"count": 0, "gb": 0.0},
                                 "4-5人": {"count": 0, "gb": 0.0}}}
        elements = [
            {
                'component': 'div',
                'html': HHClubButler._overview_html(overview)
            }
        ]
        return (
            {"cols": 12, "md": 6},
            {"refresh": 60, "title": "憨憨保种区管家", "border": True},
            elements
        )

    def stop_service(self):
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
        except Exception as e:
            logger.error(f"停止插件服务失败：{e}")

    # ============================================================
    # API
    # ============================================================
    def api_run(self):
        """手动触发运行（后台执行，请求立即返回，避免进度条挂起）"""
        if self._running:
            return {"success": False, "result": "憨憨保种区管家正在运行中，请稍候"}
        threading.Thread(target=self.run, daemon=True).start()
        return {"success": True, "result": "已在后台启动优选，请查看运行日志"}

    def api_result(self):
        """查看最近结果"""
        return {"result": self._last_result}

    # ============================================================
    # 核心逻辑
    # ============================================================
    def run(self):
        """执行优选流程"""
        if self._running:
            logger.info("憨憨保种区管家正在运行中，跳过本次触发")
            return
        cookie = self._get_site_cookie()
        if not cookie:
            self._last_result = "未获取到站点Cookie（MP站点管理未配置憨憨站，或未手动填写）"
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
                    self.post_message(
                        mtype=NotificationType.Plugin,
                        title="【憨憨保种区管家】运行异常",
                        text=str(e)
                    )
                except Exception as ex:
                    logger.error(f"憨憨保种区管家异常通知发送失败：{ex}")
        finally:
            self._running = False

    def _do_run(self):
        logs = []
        logger.info(f"憨憨保种区管家运行开始（代码版本 v{self.plugin_version}）")
        logs.append("=== 憨憨保种区管家保种优选开始 ===")

        # 1. 抓取保种区全部种子
        seeds = self._fetch_rescue_all(logs)
        if not seeds:
            self._last_result = "保种区未获取到种子（检查Cookie是否有效）"
            logger.warning(self._last_result)
            self._save_log(logs)
            return
        # 全量种子标题（含0做种/不满足做种人数条件的），供自动清理兜底识别：
        # 只要任务名来自保种区，无论当前做种人数如何都能识别为本站种子
        all_site_titles = {HHClubButler._norm_title(s.get("title") or "") for s in seeds}

        # 排除0做种人数
        if self._exclude_zero:
            before = len(seeds)
            seeds = [s for s in seeds if s.get("seeders", 0) > 0]
            logs.append(f"已排除0做种种子 {before - len(seeds)} 个，剩 {len(seeds)} 个")

        # 2. 获取当前保种情况（完成页+下载器交集）
        current = self._get_current_seeding(logs)
        if current.get("error"):
            msg = f"无法获取当前保种情况（{current['error']}），为防止误推送已停止运行"
            logs.append(f"❌ {msg}")
            self._last_result = msg
            logger.warning(msg)
            self._save_log(logs)
            return
        current_pt = current.get("total_pt", 0.0)
        current_gb = current.get("total_gb", 0.0)
        logs.append(f"当前保种：{current.get('count', 0)} 个，预计每日积分 {current_pt:.1f}，体积 {current_gb:.1f} GB")

        # 缓存概况数据（设置页顶部卡片用，免二次抓取）
        try:
            self._last_overview = self._build_overview_from_current(current)
        except Exception as e:
            logger.error(f"构建概况缓存失败：{e}")

        # 3. 计算目标（按积分/按体积 二选一）
        target = self._target_pt if not self._use_volume else self._target_volume
        if not self._use_volume:
            eff_target = max(0.0, target - current_pt)
            logs.append(f"目标积分 {target}，当前保种预计 {current_pt:.1f}，差额 {eff_target:.1f}")
        else:
            eff_target = max(0.0, target - current_gb)
            logs.append(f"目标体积（总保种上限）{target}，当前保种体积 {current_gb:.1f} GB，剩余可增 {eff_target:.1f} GB")

        # 4. 做种人数条件过滤（增量/换种两种模式均生效）
        if self._seeder_cond:
            rng = HHClubButler._parse_seeder_range(self._seeder_cond)
            if rng is None:
                logs.append(f"做种人数条件无法解析：{self._seeder_cond!r}，本次按不限处理")
            else:
                before = len(seeds)
                seeds = [s for s in seeds if rng[0] <= s.get("seeders", 0) <= rng[1]]
                logs.append(f"做种人数条件 {rng[0]}~{rng[1]}：候选 {before} → {len(seeds)} 个")

        # 5. 优选（增量/换种）
        if self._mode == "wash" and current.get("seeds"):
            result = self._optimize_with_wash(seeds, current.get("seeds"), eff_target, target, logs)
        else:
            result = self._optimize_incremental(seeds, eff_target, current_gb, logs)
            result["del_seeds"] = []
            result["keep_count"] = 0

        picked = result.get("picked", [])
        # 6. 删除低效种子（择优换种）
        if result.get("del_seeds"):
            self._delete_seeds(result["del_seeds"], logs)
            for s in result["del_seeds"]:
                logger.info(f"优选删除种子: {s.get('title','')} | "
                            f"{s.get('size',0.0):.1f} GB | "
                            f"初始做种 {s.get('seeders','?')} 人 | "
                            f"-{s.get('daily_pt',0.0):.1f} 积分")
        # 6.5 过滤已在下载器中的种子，避免重复推送（取全部种子含下载中/暂停/tracker缺失的，
        #     不按tracker筛选——tracker字段可能因暂停未连接等缺失，按tracker筛选会漏掉已在下载器的种子）
        filtered = 0
        if picked:
            dl_now = self._get_downloader_seeds(logs, only_completed=False, any_tracker=True)
            if dl_now is not None:
                # 归一化标题比较：站点标题与下载器任务名常有空格/点/括号差异，原始文本匹配会漏
                dl_norm = {HHClubButler._norm_title(n) for n in dl_now}
                before = len(picked)
                picked = [s for s in picked if HHClubButler._norm_title(s["title"]) not in dl_norm]
                filtered = before - len(picked)
                if filtered:
                    logs.append(f"已过滤 {filtered} 个已在下载器的种子，实际推送 {len(picked)} 个")
        # 7. 推送新增种子
        ok_count = fail_count = 0
        fail_list = []
        if picked:
            ok_count, fail_count, fail_list = self._push_seeds(picked, logs)
            for s in picked:
                logger.info(f"优选新增种子: {s.get('title','')} | "
                            f"{s.get('size',0.0):.1f} GB | "
                            f"初始做种 {s.get('seeders','?')} 人 | "
                            f"+{s.get('daily_pt',0.0):.1f} 积分")
        else:
            logs.append("无需新增下载")

        # 推送失败记录与告警：run_log 汇总行 + 系统日志 error 级（便于排查）
        if fail_list:
            logs.append(f"❌ 推送失败 {len(fail_list)} 个：")
            for f in fail_list:
                logger.error(f"推送失败种子: {f['title']} | {f['reason']}")
                logs.append(f"   {f['title']}（{f['reason']}）")

        # 7.5 自动清理超时未完成的下载任务（本站相关：tracker匹配/本站标签/保种区种子名三重识别）
        logger.info(f"自动清理配置：{self._auto_clean_days:g} 天（{'启用' if self._auto_clean_days > 0 else '未启用'}）")
        cleaned = 0
        if self._auto_clean_days > 0:
            cleaned = self._clean_stale_downloads(logs, seeds, all_site_titles)

        # 汇总（按实际推送的种子重算积分/体积——去重过滤后可能与优选结果不同）
        total_pt = sum(s.get("daily_pt", 0.0) for s in picked)
        total_gb = sum(s.get("size", 0.0) for s in picked)
        mode_name = "换种" if self._mode == "wash" else "增量"
        summary = (f"{mode_name}优选 {len(picked)} 个 | "
                   f"新增积分 {total_pt:.1f} | "
                   f"新增体积 {total_gb:.1f} GB")
        if result.get("del_seeds"):
            summary += f" | 删除 {len(result['del_seeds'])} 个"
        logs.append("=== " + summary + " ===")
        self._last_result = summary
        self._save_log(logs)
        logger.info(f"憨憨保种区管家优选完成：{summary}")
        if self._notify:
            try:
                notify = self._build_notify(mode_name, current, current_pt, current_gb,
                                            target, eff_target, picked, total_pt, total_gb,
                                            filtered, ok_count, fail_count, fail_list,
                                            result, cleaned)
                self.post_message(
                    mtype=NotificationType.Plugin,
                    title=f"【憨憨保种区管家】{mode_name}优选完成",
                    text="\n".join(notify)
                )
                logger.info("憨憨保种区管家通知已发送（按MP『消息通知』配置的渠道分发：企微/Telegram/站内信等）")
            except Exception as e:
                logger.error(f"憨憨保种区管家发送通知失败：{e}")
        else:
            logger.info("憨憨保种区管家通知未开启（『发送通知』开关为关），已跳过发送")

    def _build_notify(self, mode_name: str, current: dict, current_pt: float,
                      current_gb: float, target: float, eff_target: float,
                      picked: list, total_pt: float, total_gb: float,
                      filtered: int, ok_count: int, fail_count: int,
                      fail_list: list, result: dict, cleaned: int = 0) -> list:
        """构造精简通知正文（手机阅读友好，去掉过程性日志）"""
        lines = ["──────────────"]
        lines.append(f"当前保种：{current.get('count', 0)} 个")
        lines.append(f"预计积分：{current_pt:.1f} / 日")
        if self._use_volume:
            lines.append(f"目标体积：{target:.0f} GB（剩余可增 {eff_target:.1f} GB）")
        else:
            lines.append(f"目标积分：{target:.0f}（差额 {eff_target:.1f}）")
        lines.append(f"保种体积：{current_gb:.1f} GB")
        lines.append("──────────────")
        if picked:
            lines.append(f"新增 {len(picked)} 个种子：+{total_pt:.1f} 积分/日 · +{total_gb:.1f} GB")
            for s in picked[:5]:
                t = (s.get("title") or "").strip()
                t = t if len(t) <= 36 else t[:36] + "…"
                lines.append(f"  · {t}")
            if len(picked) > 5:
                lines.append(f"  …等共 {len(picked)} 个")
            if fail_count:
                lines.append(f"⚠️ 推送失败：{fail_count} 个（成功 {ok_count}）")
                for f in fail_list[:5]:
                    t = (f.get('title') or '').strip()
                    t = t if len(t) <= 30 else t + "…"
                    lines.append(f"  ✗ {t}（{f.get('reason','')}）")
                if len(fail_list) > 5:
                    lines.append(f"  …等共 {len(fail_list)} 个失败")
            else:
                lines.append(f"推送成功：{ok_count}/{len(picked)}")
        else:
            lines.append("无需新增下载")
            if filtered > 0:
                lines.append(f"已过滤 {filtered} 个已在下载器的种子")
            elif eff_target <= 0:
                lines.append("当前保种已达标")
        if result.get("del_seeds"):
            lines.append(f"删除低效种子：{len(result['del_seeds'])} 个")
            for s in result["del_seeds"][:5]:
                t = (s.get("title") or "").strip()
                t = t if len(t) <= 32 else t[:32] + "…"
                lines.append(f"  - {t}（做种{s.get('seeders','?')}人/{s.get('size',0.0):.0f}GB）")
            if len(result["del_seeds"]) > 5:
                lines.append(f"  …等共 {len(result['del_seeds'])} 个")
        if cleaned > 0:
            lines.append(f"清理超时未完成：{cleaned} 个（含未完成文件）")
        return lines

    # ============================================================
    # 站点抓取
    # ============================================================
    def _get_mp_site(self):
        """从MP站点管理自动获取憨憨站配置（cookie/ua/url），无需手动填"""
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
        """获取站点Cookie：优先MP站点管理，其次配置页手动填写"""
        if not self._cookie:
            site = self._get_mp_site()
            if site and site.cookie:
                logger.info("已从MP站点管理自动获取憨憨站Cookie")
                return site.cookie
        return self._cookie or ""

    def _get_site_url(self) -> str:
        """获取站点地址：优先MP站点管理，其次配置页"""
        url = self._site_url
        if not url or url == "https://hhanclub.net":
            site = self._get_mp_site()
            if site and site.url:
                url = site.url.rstrip("/")
        return url

    def _update_cfg(self, **kwargs):
        """合并式更新配置：MP的update_config是全量覆盖，直接调用会把其他配置项清空。
        这里先读取当前完整配置，合并要修改的键后再整体写回。"""
        cfg = self.get_config() or {}
        if not isinstance(cfg, dict):
            cfg = {}
        cfg.update(kwargs)
        self.update_config(cfg)

    def _get_site_uid(self) -> Optional[str]:
        """获取站点UID：优先配置缓存值，其次访问站点主页自动解析（NexusPHP导航含 userdetails.php?id=xxx）

        自动获取失败时不再回退任何默认值（默认UID属于他人账号，误用会拉错完成列表），
        返回 None 由调用方中止运行。"""
        if self._uid:
            return self._uid
        try:
            session = self._session()
            url = self._get_site_url()
            r = session.get(url, timeout=15)
            m = re.search(r"userdetails\.php\?id=(\d+)", r.text)
            if m:
                uid = m.group(1)
                if self._uid != uid:
                    self._uid = uid
                    try:
                        # 自动获取成功即回写配置缓存（合并式更新，不覆盖其他配置项）
                        self._update_cfg(uid=uid)
                    except Exception as e:
                        logger.error(f"UID自动回写配置失败：{e}")
                logger.info(f"已从站点主页自动获取UID：{uid}")
                return uid
        except Exception as e:
            logger.error(f"自动获取站点UID失败：{e}")
        logger.warning("未自动获取到站点UID，为防止误用他人UID已停止运行，请检查站点Cookie/UA是否有效后重试")
        return None

    def _session(self) -> requests.Session:
        s = requests.Session()
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        }
        # 优先用MP站点管理的UA（部分站点校验UA）
        site = self._get_mp_site()
        if site and site.ua:
            headers["User-Agent"] = site.ua
        cookie = self._get_site_cookie()
        if cookie:
            headers["Cookie"] = cookie
        s.headers.update(headers)
        return s

    def _fetch_rescue_all(self, logs: list) -> List[dict]:
        """抓取保种区全部页面种子"""
        seeds = []
        session = self._session()
        max_page = 1
        # 先取第一页确定总页数
        site_url = self._get_site_url()
        try:
            r = session.get(f"{site_url}/rescue.php?page=0", timeout=30)
            r.raise_for_status()
            html = r.text
            page_seeds, max_page = self._parse_rescue_page(html)
            seeds.extend(page_seeds)
            logs.append(f"保种区第1页获取 {len(page_seeds)} 条数据")
        except Exception as e:
            # 域名容错：主域不可解析时尝试备用域
            alt = site_url.replace("hhanclub.net", "hhancclub.net") if "hhanclub.net" in site_url                 else site_url.replace("hhancclub.net", "hhanclub.net")
            if alt != site_url:
                try:
                    r = session.get(f"{alt}/rescue.php?page=0", timeout=30)
                    r.raise_for_status()
                    html = r.text
                    site_url = alt
                    page_seeds, max_page = self._parse_rescue_page(html)
                    seeds.extend(page_seeds)
                    logs.append(f"保种区第1页已通过备用域名访问：{alt}，获取 {len(page_seeds)} 条数据")
                except Exception as e2:
                    logs.append(f"保种区第1页获取失败（含备用域名）：{e2}")
                    return []
            else:
                logs.append(f"保种区第1页获取失败：{e}")
                return []
        # 翻页
        for page in range(1, max_page + 1):
            try:
                r = session.get(f"{self._get_site_url()}/rescue.php?page={page}", timeout=30)
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
        """解析保种区单页，返回(种子列表, 最大页码)"""
        seeds = []
        max_page = 1
        soup = BeautifulSoup(html, "html.parser")
        # 分页链接：rescue.php?page=N（0基）
        for a in soup.select('a[href*="page="]'):
            m = re.search(r'[?&]page=(\d+)', a.get("href", ""))
            if m:
                max_page = max(max_page, int(m.group(1)))
        # 种子行
        for row in soup.select("div.torrent-table-sub-info"):
            s = self._parse_seed_row(row)
            if s:
                seeds.append(s)
        return seeds, max_page

    def _parse_seed_row(self, row) -> Optional[dict]:
        """解析单个种子行（带兜底，避免选择器匹配失败丢种子）"""
        a_title = row.select_one("a.torrent-info-text-name")
        title = a_title.get_text(strip=True) if a_title else ""
        if not title:
            # 兜底：任意包含种子的链接文本
            for a in row.find_all("a"):
                t = a.get_text(strip=True)
                if len(t) > 10:
                    title = t
                    break
        a_dl = row.select_one('a[href*="download.php"]')
        href = a_dl.get("href", "") if a_dl else ""
        # 提取大小与数字（做种人数）
        size = None
        nums = []
        # 主路径：统计区叶子文本
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
        # 兜底：全行找大小文本
        if size is None:
            for el in row.find_all(True):
                txt = el.get_text(strip=True)
                if el.find(True) is None and re.search(r'^\s*[\d.]+\s*(TB|GB|MB|KB)\s*$', txt, re.I):
                    size = size_to_gb(txt)
                    break
        if size is None:
            return None
        seeders = int(nums[0]) if nums else 0
        bean_mul, pt_mul = multiplier(seeders)
        if pt_mul <= 0:
            return None
        return {
            "title": title,
            "href": href,
            "size": size,
            "seeders": seeders,
            "bean_mul": bean_mul,
            "pt_mul": pt_mul,
            "daily_pt": C_PT * size * HOURS * pt_mul,
            "daily_bean": C_BEAN * size * HOURS * bean_mul,
            "pt_per_gb": C_PT * HOURS * pt_mul,
        }

    def _get_current_seeding(self, logs: list) -> dict:
        """获取当前保种情况：完成页 ∩ 下载器做种中"""
        result = {"count": 0, "total_pt": 0.0, "total_gb": 0.0, "seeds": []}
        # 1. 抓完成页
        completed = self._fetch_completed(logs)
        if not completed:
            logs.append("完成页未获取到数据")
            result["error"] = "完成页未获取到数据（站点Cookie失效或域名不可达）"
            return result
        # 2. 取下载器做种种子
        dl_seeds = self._get_downloader_seeds(logs)
        if dl_seeds is None:
            logs.append("下载器未获取到种子")
            result["error"] = "下载器未获取到种子（检查下载器配置/连接）"
            return result
        # 3. 交集（种子名归一化模糊匹配）
        dl_names = set(dl_seeds)
        norm_map = {}
        for c in completed:
            norm_map.setdefault(HHClubButler._norm_title(c["title"]), c)
        matched_map = {}
        matched_dl = set()
        for dn in dl_names:
            nd = HHClubButler._norm_title(dn)
            hit = None
            if nd in norm_map:
                hit = norm_map[nd]
            elif len(nd) >= 15:
                # 长名包含匹配（如完成页名带额外后缀）
                for nd_key, c in norm_map.items():
                    if len(nd_key) >= 15 and (nd in nd_key or nd_key in nd):
                        hit = c
                        break
            if hit is not None:
                matched_dl.add(dn)
                matched_map.setdefault(hit["title"], hit)
        matched = list(matched_map.values())
        result["count"] = len(matched)
        result["total_pt"] = sum(c["daily_pt"] for c in matched)
        result["total_gb"] = sum(c["size"] for c in matched)
        result["seeds"] = matched
        logs.append(f"完成页 {len(completed)} 个 ∩ 下载器做种 {len(dl_names)} 个 = 当前保种 {len(matched)} 个")
        # 未匹配明细（前10条），便于定位名称格式差异
        if matched and (len(matched) < min(len(completed), len(dl_names))):
            miss_dl = sorted(dl_names - matched_dl)[:10]
            miss_c = [c["title"] for c in completed
                      if c["title"] not in matched_map][:10]
            if miss_dl:
                logs.append("未匹配（下载器侧）前10：" + " | ".join(miss_dl))
            if miss_c:
                logs.append("未匹配（完成页侧）前10：" + " | ".join(miss_c))
        if not matched and completed and dl_names:
            result["error"] = ("完成页与下载器交集为空（种子名匹配失败），"
                               "为防止误推送已停止，请查看日志确认名称格式")
            logs.append("❌ 交集为空且双方均有数据，判定为名称匹配异常")
        return result

    def _build_overview_from_current(self, cur: dict) -> dict:
        """由当前保种结果直接构建概况（口径与 _build_seeding_overview 一致，免二次抓取）"""
        seeds = cur.get("seeds", [])
        dist = {
            "0-1人": {"count": 0, "gb": 0.0},
            "2-3人": {"count": 0, "gb": 0.0},
            "4-5人": {"count": 0, "gb": 0.0},
        }
        total_bean = 0.0
        for s in seeds:
            n = s.get("seeders", 0)
            if n <= 1:
                key = "0-1人"
            elif n <= 3:
                key = "2-3人"
            else:
                key = "4-5人"
            dist[key]["count"] += 1
            dist[key]["gb"] += s.get("size", 0.0)
            total_bean += s.get("daily_bean", 0.0)
        total_gb = cur.get("total_gb", 0.0)
        return {
            "count": cur.get("count", 0),
            "total_gb": total_gb,
            "total_tb": total_gb / 1024.0,
            "total_bean": total_bean,
            "total_pt": cur.get("total_pt", 0.0),
            "dist": dist,
            "ok": cur.get("count", 0) > 0 or bool(seeds),
        }

    def _build_seeding_overview(self, logs: list) -> dict:
        """今日保种概况：完成页 ∩ 下载器做种交集，按初始做种人数分档统计"""
        cur = self._get_current_seeding(logs)
        seeds = cur.get("seeds", [])
        dist = {
            "0-1人": {"count": 0, "gb": 0.0},
            "2-3人": {"count": 0, "gb": 0.0},
            "4-5人": {"count": 0, "gb": 0.0},
        }
        total_bean = 0.0
        for s in seeds:
            n = s.get("seeders", 0)
            if n <= 1:
                key = "0-1人"
            elif n <= 3:
                key = "2-3人"
            else:
                key = "4-5人"
            dist[key]["count"] += 1
            dist[key]["gb"] += s.get("size", 0.0)
            total_bean += s.get("daily_bean", 0.0)
        total_gb = cur.get("total_gb", 0.0)
        return {
            "count": cur.get("count", 0),
            "total_gb": total_gb,
            "total_tb": total_gb / 1024.0,
            "total_bean": total_bean,
            "total_pt": cur.get("total_pt", 0.0),
            "dist": dist,
            "ok": cur.get("count", 0) > 0 or seeds or cur.get("error"),
        }

    def _fetch_completed(self, logs: list) -> List[dict]:
        """抓取完成的保种区种子（userdetails.php?id=xxx&action=7）"""
        uid = self._get_site_uid()
        if not uid:
            logs.append("未获取到站点UID（自动解析失败且未手动填写），为防止误用他人UID已停止运行")
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
            # 域名容错：主域不可解析时尝试备用域（hhancclub.net <-> hhanclub.net）
            alt = url.replace("hhanclub.net", "hhancclub.net") if "hhanclub.net" in url                 else url.replace("hhancclub.net", "hhanclub.net")
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
        """种子名归一化：小写 + 仅保留字母/数字/中文，消除所有分隔符、括号与符号差异。

        站点完成页与下载器（QB/TR）的种子名常存在空格/点/连字符/下划线/括号等
        差异（如 The.Ball.Bing vs The Ball Bing、H.265 vs H 265），全部剥离后
        仅剩字母数字中文，可最大程度消除漏匹配。"""
        if not s:
            return ""
        s = str(s).lower()
        return re.sub(r'[^0-9a-z\u4e00-\u9fff]', '', s)

    def _parse_completed_page(self, html: str) -> Tuple[List[dict], int]:
        """解析完成页，返回(种子列表, 最大页码)"""
        items = []
        max_page = 0
        soup = BeautifulSoup(html, "html.parser")
        # 最大页码：页面内置 var maxpage=N（0基）
        m = re.search(r"var\s+maxpage\s*=\s*(\d+)", html)
        if m:
            max_page = int(m.group(1))
        # 找表格
        table = None
        for tb in soup.find_all("table"):
            if "种子ID" in tb.get_text() and tb.select("tr.text-center"):
                table = tb
                break
        if not table:
            return items, max_page
        # 表头列映射
        header_tr = table.find("tr")
        headers = [th.get_text(strip=True) for th in header_tr.find_all(["th", "td"])] if header_tr else []
        idx_size = 2
        idx_n = 3
        for i, h in enumerate(headers):
            if "大小" in h:
                idx_size = i
            if "初始保种" in h:
                idx_n = i
        # 数据行
        for tr in table.select("tr.text-center"):
            tds = tr.find_all("td")
            if len(tds) <= max(idx_size, idx_n):
                continue
            a = tds[1].find("a") if len(tds) > 1 else None
            title = a.get_text(strip=True) if a else tds[1].get_text(strip=True)
            size = size_to_gb(tds[idx_size].get_text(strip=True))
            n_str = tds[idx_n].get_text(strip=True)
            try:
                n = int(n_str)
            except ValueError:
                n = 1
            if not size:
                continue
            bean_mul, pt_mul = multiplier(n)
            if pt_mul <= 0:
                continue
            items.append({
                "title": title,
                "size": size,
                "seeders": n,
                "bean_mul": bean_mul,
                "pt_mul": pt_mul,
                "daily_pt": C_PT * size * HOURS * pt_mul,
                "daily_bean": C_BEAN * size * HOURS * bean_mul,
                "pt_per_gb": C_PT * HOURS * pt_mul,
            })
        return items, max_page

    # ============================================================
    # 下载器操作
    # ============================================================
    def _get_downloader_obj(self):
        """获取下载器实例"""
        if not self._downloader:
            return None
        try:
            services = DownloaderHelper().get_services(name_filters=[self._downloader])
            if not services:
                logger.warning(f"下载器 {self._downloader} 未配置")
                return None
            service = services.get(self._downloader)
            if not service or not service.instance:
                logger.warning(f"下载器 {self._downloader} 实例获取失败")
                return None
            if service.instance.is_inactive():
                logger.warning(f"下载器 {self._downloader} 未连接")
                return None
            return service
        except Exception as e:
            logger.error(f"获取下载器 {self._downloader} 失败：{e}")
            return None

    def _get_downloader_seeds(self, logs: list, only_completed: bool = True,
                              any_tracker: bool = False) -> Optional[set]:
        """获取下载器中种子名集合

        only_completed=True（默认，当前保种/概况统计用）：只统计已下载完成（进度100%）的种子，
        未下载完成的不参与做种、不计入；
        only_completed=False（推送前去重用）：返回全部种子名（含下载中、暂停、tracker缺失的），
        避免重复推送同一种子；any_tracker=True 时不按 tracker 筛选（tracker 字段可能因
        暂停未连接/站点tracker替换等而缺失，按 tracker 筛选会漏掉已在下载器的种子）。"""
        service = self._get_downloader_obj()
        if not service:
            logs.append("未配置有效的下载器")
            return None
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
                    if isinstance(t, dict):
                        tracker = str(t.get("tracker") or t.get("tracker_v2")
                                      or t.get("trackers") or "")
                    else:
                        tracker = str(getattr(t, "tracker", "") or getattr(t, "tracker_v2", "")
                                      or getattr(t, "trackers", "") or "")
                except Exception:
                    pass
                if any_tracker or ("hhanclub" in tracker or "hhclub" in tracker
                                   or "hanclub" in tracker):
                    site_total += 1
                    # 已完成判定：progress 必须为 1.0（100%）才算下载完成
                    if only_completed:
                        progress = None
                        try:
                            if isinstance(t, dict):
                                progress = t.get("progress")
                            else:
                                progress = getattr(t, "progress", None)
                                if progress is None:
                                    progress = getattr(t, "percent_done", None)
                        except Exception:
                            progress = None
                        if progress is not None:
                            try:
                                progress = float(progress)
                            except (TypeError, ValueError):
                                progress = None
                        if progress is not None and progress < 1.0:
                            continue
                    try:
                        name = t.get("name") if isinstance(t, dict) else getattr(t, "name", "")
                    except Exception:
                        name = ""
                    if name:
                        names.add(name)
            if only_completed:
                logs.append(f"下载器共 {len(torrents)} 个种子，本站tracker {site_total} 个，已完成做种 {len(names)} 个")
            else:
                logs.append(f"下载器全部种子 {len(names)} 个（含下载中/暂停/tracker缺失，用于推送去重）")
            if not any_tracker and site_total == 0:
                logs.append("下载器未匹配到本站tracker种子（tracker字段缺失或格式异常），为避免误算已中止")
                return None
            if not names:
                if only_completed:
                    logs.append("下载器本站种子均未下载完成，当前无已完成做种种子，按0处理")
                return set()
            return names
        except Exception as e:
            logs.append(f"获取下载器种子失败：{e}")
            return None
    def _push_seeds(self, seeds: list, logs: list):
        """推送种子到下载器，返回 (成功数, 失败数, 失败明细列表)"""
        service = self._get_downloader_obj()
        if not service:
            logs.append("未配置有效的下载器，无法推送")
            return 0, 0, []
        session = self._session()
        ok_count = 0
        fail_list = []
        for s in seeds:
            href = s.get("href", "")
            title = s.get("title", "")
            if not href:
                fail_list.append({"title": title, "reason": "缺少下载链接"})
                logs.append(f"❌ 缺少下载链接：{title}")
                continue
            url = href if href.startswith("http") else f"{self._get_site_url()}/{href.lstrip('/')}"
            try:
                # 用站点Cookie下载种子文件内容，直接喂给下载器（避免下载器无Cookie导致403）
                r = session.get(url, timeout=60)
                if r.status_code == 200 and r.content:
                    dl_type = ""
                    try:
                        dl_type = service.config.type or ""
                    except Exception:
                        pass
                    kwargs = {}
                    if self._tag:
                        if "Qbittorrent" in str(dl_type):
                            kwargs["tag"] = self._tag
                        elif "Transmission" in str(dl_type):
                            kwargs["labels"] = [self._tag]
                        else:
                            kwargs["tag"] = self._tag
                    success = service.instance.add_torrent(
                        content=r.content,
                        download_dir=self._save_path or None,
                        cookie=self._get_site_cookie() or None,
                        **kwargs
                    )
                    if success:
                        ok_count += 1
                        logs.append(f"✅ 已添加：{title}")
                    else:
                        fail_list.append({"title": title, "reason": "下载器拒绝添加（可能已存在相同任务）"})
                        logs.append(f"❌ 添加失败：{title}")
                else:
                    fail_list.append({"title": title, "reason": f"种子文件下载失败(HTTP {r.status_code})"})
                    logs.append(f"❌ 种子下载失败({r.status_code})：{title}")
            except Exception as e:
                fail_list.append({"title": title, "reason": f"推送异常：{e}"})
                logs.append(f"❌ 推送异常：{title} - {e}")
            time.sleep(1)
        logs.append(f"推送完成：成功 {ok_count}/{len(seeds)}")
        return ok_count, len(seeds) - ok_count, fail_list
    def _delete_seeds(self, seeds: list, logs: list):
        """删除被换出的低效保种种子：任务与文件一起删除（释放硬盘空间）。
        匹配用归一化模糊匹配（与当前保种交集口径一致），防止格式差异漏删。"""
        service = self._get_downloader_obj()
        if not service:
            logs.append("未配置有效的下载器，无法删除")
            return
        try:
            torrents, error = service.instance.get_torrents()
            if error:
                logs.append("获取下载器种子列表出错，无法删除")
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
                logs.append(f"已删除 {len(del_ids)} 个低效种子（任务+文件）")
                for t in torrents:
                    try:
                        tname = t.get("name") if isinstance(t, dict) else getattr(t, "name", "")
                        thash = (t.get("hash") if isinstance(t, dict)
                                 else getattr(t, "hash", None) or getattr(t, "hashString", ""))
                    except Exception:
                        continue
                    if thash in del_ids:
                        logs.append(f"  - 实际删除: {tname}")
        except Exception as e:
            logs.append(f"删除种子失败：{e}")

    @staticmethod
    def _norm_match(name: str, targets: set) -> bool:
        """归一化匹配：完全相等，或长名（>=15字符）互相包含。与计算当前保种
        (_get_current_seeding) 的交集口径一致，避免下载器任务名与站点标题
        存在空格/点/括号等格式差异时漏删。"""
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
        """自动清理超过 N 天仍未下载完成的任务（仅限本站相关，防误删其他站）

        本站识别：
        1. tracker 字段含憨憨站关键词（hhanclub/hhclub/hanclub）；
        2. magnet_uri 磁力链含本站 tracker 域名（tracker 字段在暂停/从未联系时为空，
           而 magnet_uri 对任何任务都存在且必带 tr= 参数，是最可靠的兜底）；
        3. 任务名归一化后命中本次保种区全量种子标题。
        删除动作：连未完成文件一起删除（半成品无保留价值）。"""
        service = self._get_downloader_obj()
        if not service:
            logs.append("未配置有效的下载器，跳过未完成清理")
            logger.info("未完成清理：未配置有效的下载器，跳过")
            return 0
        try:
            torrents, error = service.instance.get_torrents()
            if error:
                logs.append("获取下载器种子列表出错，跳过未完成清理")
                logger.info("未完成清理：获取下载器种子列表出错，跳过")
                return 0
        except Exception as e:
            logs.append(f"获取下载器种子失败：{e}")
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
        stat_total = 0
        stat_site = 0
        stat_done = 0
        stat_noadd = 0
        stat_probe = 0
        for t in torrents:
            try:
                if isinstance(t, dict):
                    name = str(t.get("name") or "")
                    tracker = str(t.get("tracker") or t.get("tracker_v2") or t.get("trackers") or "")
                    magnet = str(t.get("magnet_uri") or "")
                    progress = t.get("progress")
                    added = t.get("added_on") or t.get("added_time") or 0
                    tags = str(t.get("tags") or t.get("labels") or "")
                    tid = t.get("hash") or t.get("id")
                    raw_keys = list(t.keys()) if stat_probe < 3 else None
                else:
                    name = str(getattr(t, "name", "") or "")
                    tracker = str(getattr(t, "tracker", "") or getattr(t, "tracker_v2", "")
                                  or getattr(t, "trackers", "") or "")
                    magnet = str(getattr(t, "magnet_uri", "") or "")
                    progress = getattr(t, "progress", None)
                    added = getattr(t, "added_on", 0) or getattr(t, "added_time", 0) or 0
                    tags = str(getattr(t, "tags", "") or getattr(t, "labels", "") or "")
                    tid = getattr(t, "hash", None) or getattr(t, "hashString", None) or getattr(t, "id", None)
                    raw_keys = None
            except Exception:
                continue
            stat_total += 1
            # 本站识别（tracker 字段在暂停/从未联系时为空，此时靠 magnet_uri 里的 announce 地址兜底；
            # magnet_uri 对任何任务都存在且必带 tr= 参数，含本站 tracker 域名即视为本站任务）
            is_site = ("hhanclub" in tracker or "hhclub" in tracker or "hanclub" in tracker
                       or "hhanclub" in magnet or "hhclub" in magnet or "hanclub" in magnet)
            if not is_site and site_titles:
                n = HHClubButler._norm_title(name)
                if n and n in site_titles:
                    is_site = True
            # 诊断：打印打黑/绝命等疑似测试任务的关键字段
            low = name.lower()
            if ("打黑" in name or "black.storm" in low or "绝命" in name
                    or "kill" in low or "test" in low or "测试" in name) and stat_probe < 6:
                stat_probe += 1
                tracker_host = tracker.split("/")[0] if tracker else ""
                logger.info(f"清理诊断[{stat_probe}] name={name[:60]!r} tracker_host={tracker_host!r} "
                            f"magnet_has_hh={'hhanclub' in magnet or 'hhclub' in magnet} "
                            f"tags={tags!r} progress={progress!r} added={added!r} is_site={is_site} "
                            f"keys={raw_keys}")
            if not is_site:
                continue
            stat_site += 1
            try:
                progress = float(progress) if progress is not None else 1.0
            except (TypeError, ValueError):
                progress = 1.0
            if progress >= 1.0:
                stat_done += 1
                continue  # 已下载完成的不动
            try:
                added = float(added)
            except (TypeError, ValueError):
                added = 0.0
            if added <= 0:
                stat_noadd += 1
                continue  # 无添加时间信息，不处理
            if now - added >= threshold:
                stale.append((tid, name))
        logger.info(f"未完成清理：共扫描 {stat_total} 个任务，本站识别 {stat_site} 个"
                    f"（完成跳过 {stat_done}、无添加时间 {stat_noadd}、超时 {len(stale)}）")
        if not stale:
            logs.append(f"未完成清理：无超过 {self._auto_clean_days:g} 天未完成的任务")
            logger.info(f"未完成清理：扫描完成，无超过 {self._auto_clean_days:g} 天的未完成任务")
            return 0
        ids = [tid for tid, _ in stale if tid]
        if not ids:
            logs.append(f"未完成清理：识别到 {len(stale)} 个超时任务但无法取得ID，跳过")
            return 0
        try:
            service.instance.delete_torrents(delete_file=True, ids=ids)
            logs.append(f"未完成清理：已删除 {len(ids)} 个超过 {self._auto_clean_days:g} 天未完成的任务（含未完成文件）")
            logger.info(f"未完成清理：已删除 {len(ids)} 个超过 {self._auto_clean_days:g} 天的未完成任务")
            return len(ids)
        except Exception as e:
            logs.append(f"未完成清理失败：{e}")
            logger.info(f"未完成清理失败：{e}")
            return 0

    # ============================================================
    # 优选算法
    # ============================================================
    @staticmethod
    def _parse_seeder_range(raw) -> Optional[tuple]:
        """解析做种人数条件：单值"3"=恰好3人；区间"0-2"=0~2人。返回(min,max)，解析失败返回None"""
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

    def _optimize_incremental(self, seeds: list, eff_target: float, current_gb: float, logs: list) -> dict:
        """增量优选：不删除已做种任务。按积分=补齐差额最小体积；按体积=剩余空间内积分最大化"""
        if self._use_volume:
            cap = max(0.0, self._target_volume - current_gb)
            if cap <= 0:
                logs.append("当前保种体积已达目标上限，无需新增")
                return {"picked": [], "total_pt": 0.0, "total_gb": 0.0, "del_seeds": [], "keep_count": 0}
            logs.append(f"按体积增量：剩余可增 {cap:.0f} GB（目标总保种 {self._target_volume:.0f} GB"
                        f" - 已保种 {current_gb:.0f} GB），新增种子积分最大化")
            picked = HHClubButler._maximize_pt_with_cap(seeds, cap)
            total_pt = sum(s["daily_pt"] for s in picked)
            total_gb = sum(s["size"] for s in picked)
            logs.append(f"增量优选 {len(picked)} 个，新增积分 {total_pt:.1f}，新增体积 {total_gb:.1f} GB")
            return {"picked": picked, "total_pt": total_pt, "total_gb": total_gb,
                    "del_seeds": [], "keep_count": 0}
        # 按积分
        if eff_target <= 0:
            logs.append("当前保种积分已达标，无需新增下载")
            return {"picked": [], "total_pt": 0.0, "total_gb": 0.0, "del_seeds": [], "keep_count": 0}
        logs.append(f"按积分增量：差额 {eff_target:.1f} 积分，最小体积达标")
        opt = HHClubButler._optimize(seeds, eff_target)
        opt["del_seeds"] = []
        opt["keep_count"] = 0
        logs.append(f"增量优选 {len(opt['picked'])} 个，新增积分 {opt['total_pt']:.1f}，"
                    f"新增体积 {opt['total_gb']:.1f} GB")
        return opt

    @staticmethod
    def _maximize_pt_with_cap(seeds: list, cap: float) -> list:
        """体积上限内积分最大化（0/1背包：dp[j]=达到积分j的最小体积，找 dp[j]<=cap 的最大j）"""
        if cap <= 0 or not seeds:
            return []
        SCALE = 10
        n = len(seeds)
        total_w = sum(int(round(s["daily_pt"] * SCALE)) for s in seeds)
        if total_w <= 0:
            return []
        dp_len = total_w + 1
        if n * dp_len > 8000000:
            # 内存保护：贪心按积分效率
            picked = []
            used = 0.0
            for s in sorted(seeds, key=lambda x: x["pt_per_gb"], reverse=True):
                if used + s["size"] <= cap:
                    picked.append(s)
                    used += s["size"]
            return picked
        INF = float("inf")
        dp = [INF] * dp_len
        dp[0] = 0.0
        keep = bytearray(n * dp_len)
        for i, s in enumerate(seeds):
            w = int(round(s["daily_pt"] * SCALE))
            if w <= 0:
                continue
            v = s["size"]
            for j in range(dp_len - 1, w - 1, -1):
                if dp[j - w] + v < dp[j]:
                    dp[j] = dp[j - w] + v
                    keep[i * dp_len + j] = 1
        best_j = -1
        for j in range(dp_len - 1, -1, -1):
            if dp[j] != INF and dp[j] <= cap:
                best_j = j
                break
        if best_j < 0:
            return []
        picked = []
        j = best_j
        for i in range(n - 1, -1, -1):
            if keep[i * dp_len + j]:
                picked.append(seeds[i])
                j -= int(round(seeds[i]["daily_pt"] * SCALE))
        picked.reverse()
        return picked

    @staticmethod
    def _parse_seeder_range(raw) -> Optional[tuple]:
        """解析做种人数：单值"3"=恰好3人；区间"0-5"=0~5人。返回(min,max)，解析失败返回None"""
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
    def _optimize(seeds: list, target_pt: float, volume_cap: Optional[float] = None) -> dict:
        """DP 0/1背包：达到目标积分所需的最小体积（允许略微超出目标，体积最小优先）"""
        if target_pt <= 0:
            return {"picked": [], "total_pt": 0.0, "total_gb": 0.0}
        SCALE = 10
        n = len(seeds)
        max_j = int(target_pt * SCALE)
        if n == 0 or max_j <= 0:
            return {"picked": [], "total_pt": 0.0, "total_gb": 0.0}
        # 最大种子积分（用于扩展DP上限，允许组合略微超出目标）
        max_w = 0
        for s in seeds:
            w = int(round(s["daily_pt"] * SCALE))
            if w > max_w:
                max_w = w
        dp_len = max_j + max_w + 1
        # 内存保护：超过阈值降级贪心
        if n * dp_len > 8000000:
            return HHClubButler._greedy(seeds, target_pt)
        INF = float("inf")
        dp = [INF] * dp_len
        dp[0] = 0.0
        keep = bytearray(n * dp_len)
        for i, s in enumerate(seeds):
            w = int(round(s["daily_pt"] * SCALE))
            if w <= 0:
                continue
            v = s["size"]
            for j in range(dp_len - 1, w - 1, -1):
                if dp[j - w] + v < dp[j]:
                    dp[j] = dp[j - w] + v
                    keep[i * dp_len + j] = 1
        # 找 >= max_j 的最小体积可达点（允许略微超出目标；有体积上限时须满足 dp[j] <= volume_cap）
        best_j = -1
        best_vol = INF
        for j in range(max_j, dp_len):
            if dp[j] != INF and dp[j] < best_vol:
                if volume_cap is None or dp[j] <= volume_cap:
                    best_vol = dp[j]
                    best_j = j
        # 有体积上限且无解：退而求其次，在上限内找积分最大的可行解（尽力达标）
        if best_j < 0 and volume_cap is not None:
            for j in range(dp_len - 1, -1, -1):
                if dp[j] != INF and dp[j] <= volume_cap:
                    best_j = j
                    break
        if best_j < 0:
            return {"picked": [], "total_pt": 0.0, "total_gb": 0.0}
        picked = []
        j = best_j
        for i in range(n - 1, -1, -1):
            if keep[i * dp_len + j]:
                picked.append(seeds[i])
                j -= int(round(seeds[i]["daily_pt"] * SCALE))
        picked.reverse()
        total_pt = sum(s["daily_pt"] for s in picked)
        total_gb = sum(s["size"] for s in picked)
        return {"picked": picked, "total_pt": total_pt, "total_gb": total_gb}

    @staticmethod
    def _greedy(seeds: list, target_pt: float) -> dict:
        """贪心降级：按每GB积分降序"""
        picked = []
        total_pt = 0.0
        total_gb = 0.0
        for s in sorted(seeds, key=lambda x: x["pt_per_gb"], reverse=True):
            if total_pt >= target_pt:
                break
            picked.append(s)
            total_pt += s["daily_pt"]
            total_gb += s["size"]
        return {"picked": picked, "total_pt": total_pt, "total_gb": total_gb}

    @staticmethod
    def _tier(seeders) -> int:
        """倍率档位：0=0-1人(最高倍率), 1=2-3人, 2=4-5人(最低倍率)"""
        if seeders <= 1:
            return 0
        if seeders <= 3:
            return 1
        return 2

    @staticmethod
    def _pick_to_cover(candidates: list, need: float) -> tuple:
        """达标补充选择：优先大种子，若大种子加入会明显超标且存在更小单颗
        能更接近达标，则跳过大种子留给小种子拼凑；拼不够时兜底补回大种子。
        返回 (选中列表, 总积分)。"""
        cand = sorted(candidates, key=lambda x: x["size"], reverse=True)
        picked = []
        cur = 0.0
        skipped = []
        n = len(cand)
        for i, s in enumerate(cand):
            if cur >= need - 1e-6:
                break
            remain = need - cur
            if cur + s["daily_pt"] >= need - 1e-6:
                # 这颗加入即达标：若后面有更小单颗也能达标且超出更少 → 跳过，留给小种精调
                better = -1
                for j in range(i + 1, n):
                    t = cand[j]
                    if t["daily_pt"] >= remain - 1e-6 and t["daily_pt"] < s["daily_pt"]:
                        if better < 0 or t["daily_pt"] < cand[better]["daily_pt"]:
                            better = j
                if better >= 0:
                    skipped.append(i)
                    continue
            picked.append(s)
            cur += s["daily_pt"]
        # 兜底：仍不足则把跳过的大种子补回，直到达标
        if cur < need - 1e-6:
            for i in skipped:
                if cur >= need - 1e-6:
                    break
                picked.append(cand[i])
                cur += cand[i]["daily_pt"]
        return picked, cur

    def _wash_volume(self, cur_by_tier: dict, cand_by_tier: dict, current: list,
                     logs: list) -> dict:
        """换种优选（按体积）：总体积 <= 目标上限，尽量高倍率。
        从高档到低档逐档：当前种子优先（从大到小塞入），当前全保留且还有
        空间才补同档候选（从大到小塞满）；塞不下的当前种子删除（任务+文件）。"""
        cap = self._target_volume
        final = []
        total_pt = 0.0
        total_gb = 0.0
        tier_names = ("0-1人", "2-3人", "4-5人")
        for t in (0, 1, 2):
            if total_gb >= cap - 1e-6:
                break
            tname = tier_names[t]
            cur_t = sorted(cur_by_tier[t], key=lambda x: x["size"], reverse=True)
            cur_picked = []
            cur_del = []
            for s in cur_t:
                if total_gb + s["size"] <= cap + 1e-6:
                    cur_picked.append(s)
                    total_gb += s["size"]
                    total_pt += s["daily_pt"]
                else:
                    cur_del.append(s)
            for s in cur_picked:
                final.append((s, False))
            if cur_del:
                # 有当前被体积上限挤出 → 同档不换，本档候选不补
                logs.append(f"{tname}档：塞入当前 {len(cur_picked)} 个，"
                            f"体积上限挤出 {len(cur_del)} 个（同档不换，不再补候选）")
                continue
            # 当前全保留且有空间 → 补同档候选（从大到小塞满）
            cand_t = sorted(cand_by_tier[t], key=lambda x: x["size"], reverse=True)
            picked_c = []
            for s in cand_t:
                if total_gb + s["size"] <= cap + 1e-6:
                    picked_c.append(s)
                    total_gb += s["size"]
                    total_pt += s["daily_pt"]
            for s in picked_c:
                final.append((s, True))
            logs.append(f"{tname}档：当前 {len(cur_picked)} 个 + 候选 {len(picked_c)} 个")
        final_titles = {s["title"] for s, _ in final}
        del_seeds = [s for s in current if s["title"] not in final_titles]
        add_seeds = [s for s, is_new in final if is_new]
        keep_count = len(final) - len(add_seeds)
        logs.append(f"换种（按体积）上限 {cap:.0f} GB：构建 {len(final)} 个"
                    f"（保留当前 {keep_count} + 新增 {len(add_seeds)}），"
                    f"删除 {len(del_seeds)} 个，最终体积 {total_gb:.1f} GB")
        for s in add_seeds:
            logs.append(f"  + 新增: {s.get('title','')} | {s.get('size',0.0):.1f} GB | "
                        f"初始做种 {s.get('seeders','?')} 人 | +{s.get('daily_pt',0.0):.1f} 积分")
        for s in del_seeds:
            logs.append(f"  - 删除: {s.get('title','')} | {s.get('size',0.0):.1f} GB | "
                        f"初始做种 {s.get('seeders','?')} 人 | -{s.get('daily_pt',0.0):.1f} 积分")
        return {
            "picked": add_seeds,
            "del_seeds": del_seeds,
            "total_pt": total_pt,
            "total_gb": total_gb,
            "keep_count": keep_count,
        }

    def _optimize_with_wash(self, candidates: list, current: list, eff_target: float,
                            target: float, logs: list) -> dict:
        """换种优选v2（按积分）：从高倍率到低倍率逐档构建最终保种集合，
        凑到目标积分即停；同档位不替换（档内优先保留当前，不足才补候选）；
        补充候选优先大种子、超标跳小灵活拼凑；集合之外及达标后多余的当前
        种子删除（任务+文件）；高倍率不够逐级用低倍率补充；全部资源仍不足
        则如实报未达标且不删除任何当前种子。"""
        cur_titles = {s["title"] for s in current}
        cur_by_tier = {0: [], 1: [], 2: []}
        for s in current:
            cur_by_tier[HHClubButler._tier(s.get("seeders", 0))].append(s)
        cand_by_tier = {0: [], 1: [], 2: []}
        for s in candidates:
            if s["title"] in cur_titles:
                continue
            cand_by_tier[HHClubButler._tier(s.get("seeders", 0))].append(s)

        if self._use_volume:
            return self._wash_volume(cur_by_tier, cand_by_tier, current, logs)

        # ---- 按积分：逐档构建 ----
        final = []          # [(seed, is_new)]
        total_pt = 0.0
        total_gb = 0.0
        tier_names = ("0-1人", "2-3人", "4-5人")
        for t in (0, 1, 2):
            tname = tier_names[t]
            # 档位内：先保留当前已保种子（同档位不替换）
            for s in cur_by_tier[t]:
                final.append((s, False))
                total_pt += s["daily_pt"]
                total_gb += s["size"]
            if total_pt >= target - 1e-6:
                logs.append(f"{tname}档当前 {len(cur_by_tier[t])} 个即达标")
                break
            # 当前不足：补充该档候选（优先大种子+灵活拼凑）
            need = target - total_pt
            cands = cand_by_tier[t]
            if cands:
                picked_c, added_pt = HHClubButler._pick_to_cover(cands, need)
                for s in picked_c:
                    final.append((s, True))
                    total_pt += s["daily_pt"]
                    total_gb += s["size"]
                logs.append(f"{tname}档：当前 {len(cur_by_tier[t])} 个 + "
                            f"候选补 {len(picked_c)} 个（+{added_pt:.1f} 积分）")
            else:
                logs.append(f"{tname}档：当前 {len(cur_by_tier[t])} 个，无候选可补")
            if total_pt >= target - 1e-6:
                break

        # 删除：未纳入 final 的当前种子
        final_titles = {s["title"] for s, _ in final}
        del_seeds = [s for s in current if s["title"] not in final_titles]
        # 本次是否有档位补充了候选（有=候选介入，保留的当前种子均为必要，不再删减）
        add_tiers = {HHClubButler._tier(s.get("seeders", 0)) for s, is_new in final if is_new}
        if total_pt >= target - 1e-6:
            if not add_tiers:
                # 未补候选即达标（当前保种本就达标/超标）：
                # 从最低档(4-5)到最高档(0-1)、档内体积从大到小删到刚好达标。
                # 只删当前已保种，绝不删新增候选。
                removable = [(s, HHClubButler._tier(s.get("seeders", 0)))
                             for s, is_new in final if not is_new]
                removable.sort(key=lambda x: (-x[1], -x[0]["size"]))
                for s, _t in removable:
                    if total_pt - s["daily_pt"] >= target - 1e-6:
                        final.remove((s, False))
                        total_pt -= s["daily_pt"]
                        total_gb -= s["size"]
                        del_seeds.append(s)
                    else:
                        break
            else:
                logs.append("本次补充候选后才达标，保留全部已保种种子（不删减，避免同档替换）")
        else:
            logs.append("⚠️ 保种区全部资源用尽仍低于目标积分，本次不删除任何当前保种种子")

        add_seeds = [s for s, is_new in final if is_new]
        keep_count = len(final) - len(add_seeds)
        logs.append(f"换种（按积分）目标 {target:.0f}：构建 {len(final)} 个"
                    f"（保留当前 {keep_count} + 新增 {len(add_seeds)}），"
                    f"删除 {len(del_seeds)} 个，最终积分 {total_pt:.1f}")
        for s in add_seeds:
            logs.append(f"  + 新增: {s.get('title','')} | {s.get('size',0.0):.1f} GB | "
                        f"初始做种 {s.get('seeders','?')} 人 | +{s.get('daily_pt',0.0):.1f} 积分")
        for s in del_seeds:
            logs.append(f"  - 删除: {s.get('title','')} | {s.get('size',0.0):.1f} GB | "
                        f"初始做种 {s.get('seeders','?')} 人 | -{s.get('daily_pt',0.0):.1f} 积分")
        return {
            "picked": add_seeds,
            "del_seeds": del_seeds,
            "total_pt": total_pt,
            "total_gb": total_gb,
            "keep_count": keep_count,
        }

    def _save_log(self, logs: list):
        """保存运行日志到插件数据目录"""
        try:
            path = self.get_data_path()
            if not path.exists():
                path.mkdir(parents=True, exist_ok=True)
            log_path = path / "run_log.txt"
            with open(log_path, "w", encoding="utf-8") as f:
                f.write("\n".join(logs))
        except Exception as e:
            logger.error(f"保存日志失败：{e}")
