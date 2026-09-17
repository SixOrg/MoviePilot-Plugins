# MoviePilot-Plugins（第三方插件市场）

MoviePilot 第三方插件仓库，当前收录：

## 憨憨保种区管家

> 自动化优选添加及换种工具（憨憨站保种区）

- 自动抓取憨憨保种区种子，按目标积分/目标体积自动化优选
- 增量优选：不删保种&补齐达标
- 换种优选：删除低效&下载高效（跨档位升级，删除任务+文件）
- 详情见 [plugins.v2/hhclubbutler/README.md](plugins.v2/hhclubbutler/README.md)

## 憨憨保种区明细导出

> 每天定时导出保种区种子明细为 Excel，用于积分公式验证与数据积累

- 默认每天 23:55 自动抓取『个人页面-完成的保种区种子』全部分页（页数自动解析，不限页数）
- 导出 Excel：Sheet1 保种区明细（10 列，与站点逐列一致）+ Sheet2 分档统计
- 支持立即运行 / 插件命令 / API 手动导出与通知，按保留天数自动清理旧文件

## 安装

MoviePilot → 设置 → 插件市场 → 添加仓库：

```
https://github.com/SixOrg/MoviePilot-Plugins
```

或环境变量：

```
PLUGIN_MARKET=https://github.com/SixOrg/MoviePilot-Plugins
```

## 免责声明

本仓库插件为个人开发作品，请自行评估使用风险；换种优选会删除下载器任务及文件，请谨慎配置。
