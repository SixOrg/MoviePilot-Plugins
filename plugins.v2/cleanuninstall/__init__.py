# -*- coding: utf-8 -*-
import os
import json
import shutil
from typing import List, Tuple, Any, Dict

from app.core.config import settings
from app.log import logger
from app.plugins import _PluginBase


class CleanUninstall(_PluginBase):
    # 插件名称
    plugin_name = "无痕卸载"
    # 插件描述
    plugin_desc = "无痕卸载插件"
    # 插件图标
    plugin_icon = "icon.png"
    # 插件版本
    plugin_version = "1.0"
    # 插件作者
    plugin_author = "六个橙子"
    # 作者主页
    author_url = "https://github.com/SixOrg"
    # 插件配置项ID前缀
    plugin_config_prefix = "cleanuninstall_"
    # 加载顺序
    plugin_order = 1
    # 可使用的用户级别
    auth_level = 1

    def init_plugin(self, config: dict = None):
        """
        初始化插件
        """
        if config:
            selected = config.get('selected_plugins') or []
            if selected:
                logger.info(f"检测到待卸载插件: {selected}")
                success = []
                failed = []
                for plugin_id in selected:
                    if self._uninstall_plugin(plugin_id):
                        success.append(plugin_id)
                    else:
                        failed.append(plugin_id)

                # 清空选中，避免重复卸载
                config['selected_plugins'] = []
                self.update_config(config)

                logger.info(f"卸载完成: 成功 {len(success)} 个, 失败 {len(failed)} 个")

    def get_state(self) -> bool:
        """
        返回插件状态：工具类插件，不需要常驻运行，返回False点击直接进设置
        """
        return False

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """
        注册远程命令
        """
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        """
        注册API
        """
        return []

    def get_service(self) -> List[Dict[str, Any]]:
        """
        注册定时服务
        """
        return []

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        配置页面：卸载功能放在这里
        """
        installed_plugins = self._get_installed_plugins()

        # 构建下拉框选项
        select_options = []
        for plugin_id in installed_plugins:
            plugin_name = self._get_plugin_name(plugin_id)
            select_options.append({
                'title': f"{plugin_name} ({plugin_id})",
                'value': plugin_id,
            })

        return [{
            'component': 'VForm',
            'content': [{
                'component': 'VRow',
                'content': [{
                    'component': 'VCol',
                    'props': {'cols': 12},
                    'content': [{
                        'component': 'VAlert',
                        'props': {
                            'type': 'warning',
                            'variant': 'tonal',
                            'text': '警告：卸载插件将删除所有配置、数据和代码文件，不可恢复！请谨慎操作。'
                        }
                    }]
                }]
            }, {
                'component': 'VRow',
                'content': [{
                    'component': 'VCol',
                    'props': {'cols': 12},
                    'content': [{
                        'component': 'VSelect',
                        'props': {
                            'model': 'selected_plugins',
                            'label': '选择要卸载的插件',
                            'multiple': True,
                            'chips': True,
                            'clearable': True,
                            'items': select_options,
                            'hint': '支持多选，选择后点击保存按钮执行卸载',
                        }
                    }]
                }]
            }]
        }], {
            'selected_plugins': [],
        }

    def get_page(self) -> List[dict]:
        """
        插件详情页：无内容，和thsrite的插件保持一致
        """
        pass

    def _get_installed_plugins(self) -> list:
        """
        获取已安装插件列表：从系统配置 UserInstalledPlugins 读取
        """
        try:
            from app.schemas.types import SystemConfigKey
            from app.application.configuration import get_configured_system_config

            config_oper = get_configured_system_config()
            installed = config_oper.get(SystemConfigKey.UserInstalledPlugins) or []

            logger.info(f"从系统配置获取到 {len(installed)} 个已安装插件")
            return installed
        except Exception as e:
            logger.error(f"获取已安装插件列表失败: {e}")
            return []

    def _get_plugin_name(self, plugin_id: str) -> str:
        """
        获取插件名称：从插件管理器的运行实例获取
        """
        try:
            from app.application.plugin.runtime import get_plugin_manager

            plugin_manager = get_plugin_manager()
            plugin_instance = plugin_manager.running_plugins.get(plugin_id)

            if plugin_instance and hasattr(plugin_instance, 'plugin_name'):
                return plugin_instance.plugin_name

            plugin_class = plugin_manager.plugins.get(plugin_id)
            if plugin_class and hasattr(plugin_class, 'plugin_name'):
                return plugin_class.plugin_name

        except Exception as e:
            logger.debug(f"获取插件名称失败 {plugin_id}: {e}")

        return plugin_id

    def _uninstall_plugin(self, plugin_id: str) -> bool:
        """
        彻底卸载插件：调用MP官方方法 + 清理数据目录
        """
        try:
            from app.application.plugin.runtime import get_plugin_manager
            from app.schemas.types import SystemConfigKey
            from app.application.configuration import get_configured_system_config
            from app.application.plugin.routes import remove_plugin_api
            from app.application.scheduling import remove_plugin_job

            plugin_manager = get_plugin_manager()

            with plugin_manager.mutation(f"卸载插件 {plugin_id}"):
                # 1. 从已安装列表移除
                config_oper = get_configured_system_config()
                install_plugins = config_oper.get(SystemConfigKey.UserInstalledPlugins) or []
                if plugin_id in install_plugins:
                    install_plugins.remove(plugin_id)
                    config_oper.set(SystemConfigKey.UserInstalledPlugins, install_plugins)

                # 2. 移除插件API和服务
                remove_plugin_api(plugin_id)
                remove_plugin_job(plugin_id)

                # 3. 停止插件
                plugin_manager.stop(plugin_id)

                # 4. 删除配置、数据、实例
                plugin_manager.delete_plugin_config(plugin_id, force=True)
                plugin_manager.delete_plugin_data(plugin_id, force=True)
                plugin_manager.delete_plugin_instance(plugin_id)

                # 5. 删除插件包文件
                if plugin_manager.remove_plugin_package(plugin_id):
                    plugin_manager.plugins.pop(plugin_id, None)

                # 6. 从插件管理器移除
                plugin_manager.remove_plugin(plugin_id)

            # 额外清理：删除数据目录和备份目录
            config_path = str(getattr(settings, 'CONFIG_PATH', ''))

            data_dir = os.path.join(config_path, 'plugins', plugin_id)
            if os.path.exists(data_dir):
                shutil.rmtree(data_dir)
                logger.info(f"已清理插件数据目录: {data_dir}")

            backup_dir = os.path.join(config_path, 'plugins_backup', plugin_id.lower())
            if os.path.exists(backup_dir):
                shutil.rmtree(backup_dir)
                logger.info(f"已清理插件备份目录: {backup_dir}")

            logger.info(f"已彻底卸载插件: {plugin_id}")
            return True

        except Exception as e:
            logger.error(f"卸载插件失败 {plugin_id}: {e}")
            return False

    def stop_service(self):
        """
        停止服务
        """
        pass
