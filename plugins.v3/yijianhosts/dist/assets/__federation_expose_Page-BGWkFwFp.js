import { importShared } from './__federation_fn_import-JrT3xvdd.js';

const _export_sfc = (sfc, props) => {
  const target = sfc.__vccOpts || sfc;
  for (const [key, val] of props) {
    target[key] = val;
  }
  return target;
};

const {createTextVNode:_createTextVNode,resolveComponent:_resolveComponent,withCtx:_withCtx,createVNode:_createVNode,createElementVNode:_createElementVNode,normalizeClass:_normalizeClass,renderList:_renderList,Fragment:_Fragment,openBlock:_openBlock,createElementBlock:_createElementBlock,toDisplayString:_toDisplayString,createCommentVNode:_createCommentVNode,createBlock:_createBlock} = await importShared('vue');


const _hoisted_1 = {
  class: "yh-root",
  style: {"width":"100%","padding":"4px 6px"}
};
const _hoisted_2 = { style: {"display":"flex","align-items":"stretch","gap":"2px"} };
const _hoisted_3 = ["title"];
const _hoisted_4 = {
  key: 0,
  style: {"margin-top":"8px"}
};
const _hoisted_5 = { style: {"display":"flex","flex-wrap":"wrap","gap":"6px 8px","align-items":"center"} };
const _hoisted_6 = {
  key: 0,
  style: {"margin-top":"10px","background":"rgba(25,118,210,0.04)","border-radius":"8px","padding":"10px 12px"}
};
const _hoisted_7 = { style: {"display":"flex","flex-wrap":"wrap","gap":"8px 10px","align-items":"center"} };
const _hoisted_8 = { style: {"width":"100%"} };
const _hoisted_9 = { style: {"display":"flex","align-items":"center","gap":"8px"} };
const _hoisted_10 = { style: {"font-size":"14px","font-weight":"600"} };
const _hoisted_11 = {
  key: 0,
  style: {"display":"flex","flex-wrap":"wrap","gap":"4px","margin-top":"6px"}
};
const _hoisted_12 = {
  key: 1,
  style: {"font-size":"12px","color":"rgba(128,128,128,0.8)","margin-top":"4px"}
};
const _hoisted_13 = { style: {"border-radius":"6px","padding":"2px 2px"} };
const _hoisted_14 = { style: {"display":"flex","align-items":"center","gap":"8px","min-height":"32px","margin-bottom":"10px"} };
const _hoisted_15 = { style: {"display":"flex","align-items":"center","gap":"8px","margin-bottom":"6px"} };
const _hoisted_16 = {
  key: 0,
  style: {"font-size":"12px","color":"rgba(128,128,128,0.75)","padding":"4px 0 0 0"}
};
const _hoisted_17 = { style: {"display":"flex","align-items":"center","gap":"8px"} };
const _hoisted_18 = { style: {"display":"flex","align-items":"center","gap":"10px","margin-top":"10px","flex-wrap":"wrap"} };
const _hoisted_19 = { style: {"display":"flex","gap":"8px","flex-shrink":"0","margin-left":"auto"} };
const _hoisted_20 = {
  key: 0,
  style: {"padding":"12px 0","color":"rgba(128,128,128,0.9)"}
};
const _hoisted_21 = {
  key: 0,
  style: {"max-height":"320px","overflow":"auto"}
};
const _hoisted_22 = { style: {"font-family":"monospace"} };
const _hoisted_23 = {
  key: 1,
  style: {"background":"rgba(0,0,0,0.05)","border-radius":"6px","padding":"10px","font-size":"13px","line-height":"1.7","white-space":"pre-wrap","word-break":"break-all","max-height":"320px","overflow":"auto"}
};

const {ref,reactive,computed,onMounted,inject} = await importShared('vue');



const _sfc_main = {
  __name: 'Page',
  props: {
  api: { type: Object, default: () => ({}) },
  pluginId: { type: String, default: '' },
  sourcePluginId: { type: String, default: '' },
},
  emits: ['action'],
  setup(__props, { emit: __emit }) {

const props = __props;
const emit = __emit;
const toast = inject('moviepilot:toast', null);

const saving = ref(false);
const runNowToggle = ref(false);
const sitePanel = ref([]);
const advPanel = ref([]);
const cronOpen = ref(false);
const cronCustom = ref(false);
const cronParts = reactive({
  min: '0', hour: '4', day: '*', month: '*', week: '*',
});
const cronFieldMin = ['0', '5', '10', '15', '20', '30', '*/5', '*/10', '*/15', '*/30'];
const cronFieldHour = ['0', '1', '2', '3', '4', '6', '8', '12', '18', '*', '*/2', '*/4', '*/6', '*/12'];
const cronFieldDay = ['*', '1', '2', '5', '10', '15', '20', '25', '28', '30', '31'];
const cronFieldMonth = ['*', '1', '3', '6', '9', '12'];
const cronFieldWeek = ['*', '0', '1', '2', '3', '4', '5', '6'];
const lastResult = ref('');
// 最近运行单行展示（推送保持分区换行模板，这里仅折叠换行为空格）
const lastResultOneLine = computed(() => (lastResult.value || '').replace(/\s*\n\s*/g, ' '));
const runResult = ref(null);
const siteItems = ref([]);
const viewDialog = ref(false);
const viewBlock = ref('');
const viewMsg = ref('');
const viewLogs = ref([]);
const viewLoading = ref(false);

const cfg = reactive({
  enabled: false,
  notify: true,
  cron: '0 4 * * *',
  custom_sites: '',
  cfst_mirror: 'https://gh-proxy.com/',
  delay_limit: '500',
  download_num: '10',
  download_limit: '0',
  retry_count: '0',
  retry_interval: '60',
  overwrite_mode: 'always',
});

const overwriteItems = [
  { text: '有结果即覆盖', value: 'always' },
  { text: '结果更优覆盖（仅当下载速度更优时覆盖）', value: 'better' },
];
const siteEnabled = reactive({});

const cronPresets = [
  { key: 'min5', label: '每5分钟', cron: '*/5 * * * *' },
  { key: 'min30', label: '每30分钟', cron: '*/30 * * * *' },
  { key: 'hour', label: '每小时', cron: '0 * * * *' },
  { key: 'day', label: '每天04:00', cron: '0 4 * * *' },
  { key: 'week', label: '每周一04:00', cron: '0 4 * * 1' },
];

// 写入明细按 IP 分组（同一优选 IP 的站点合并，仅列站点名称）
const groupedViewLogs = computed(() => {
  const groups = [];
  const byIp = new Map();
  for (const r of viewLogs.value) {
    const key = String(r.ip || '');
    if (!byIp.has(key)) {
      byIp.set(key, { ip: key, sites: [] });
      groups.push(byIp.get(key));
    }
    if (r.site && !byIp.get(key).sites.includes(r.site)) byIp.get(key).sites.push(r.site);
  }
  return groups
});

const selectedCount = computed(() => siteItems.value.filter(s => siteEnabled[s.value]).length);

// 应用自定义 cron 五级参数并收起编辑区
const applyCronCustom = () => {
  cfg.cron = `${cronParts.min} ${cronParts.hour} ${cronParts.day} ${cronParts.month} ${cronParts.week}`;
  cronCustom.value = false;
};

// 已选站点名称（仅名称，不含域名）
const selectedSiteNames = computed(() =>
  siteItems.value.filter(s => siteEnabled[s.value]).map(s => s.value));

// 自定义站点冲突检测：域名与目标站点（勾选）重复
const conflictMsgs = computed(() => {
  const msgs = [];
  const lines = String(cfg.custom_sites || '').split('\n');
  for (const line of lines) {
    const t = line.trim();
    if (!t) continue
    const parts = t.split(/\s+/);
    const name = parts.length > 1 ? parts[1] : parts[0];
    if (siteItems.value.some(s => s.value === name && siteEnabled[s.value])) {
      msgs.push(`「${name}」已在目标站点中勾选，与自定义站点重复，保存时请移除其一。`);
    }
  }
  return msgs
});

// 全选 / 反选：即时联动所有站点勾选（按钮位于面板内容工具条，不会触发折叠）
const selectAllSites = () => {
  siteItems.value.forEach(s => { siteEnabled[s.value] = true; });
};
const invertSites = () => {
  siteItems.value.forEach(s => { siteEnabled[s.value] = !siteEnabled[s.value]; });
};

async function load() {
  try {
    const data = await props.api?.get(`plugin/${props.pluginId}/get_config`);
    if (!data) return
    if (typeof data.enabled === 'boolean') cfg.enabled = data.enabled;
    if (typeof data.notify === 'boolean') cfg.notify = data.notify;
    if (data.cron !== undefined && data.cron !== null) cfg.cron = data.cron;
    cfg.custom_sites = data.custom_sites || '';
    cfg.cfst_mirror = data.cfst_mirror || '';
    if (data.delay_limit) cfg.delay_limit = String(data.delay_limit);
    if (data.download_num) cfg.download_num = String(data.download_num);
    if (data.download_limit !== undefined && data.download_limit !== null) cfg.download_limit = String(data.download_limit);
    if (data.retry_count !== undefined && data.retry_count !== null) cfg.retry_count = String(data.retry_count);
    if (data.retry_interval !== undefined && data.retry_interval !== null) cfg.retry_interval = String(data.retry_interval);
    if (data.overwrite_mode) cfg.overwrite_mode = data.overwrite_mode;
    lastResult.value = data.last_summary || data.last_result || '';
    siteItems.value = data.site_items || [];
    siteItems.value.forEach(s => { siteEnabled[s.value] = false; })
    ;(data.sites || []).forEach(n => {
      if (siteEnabled[n] !== undefined) siteEnabled[n] = true;
    });
  } catch (e) {
    console.error('加载配置失败', e);
  }
}

function selectedSites() {
  return siteItems.value.filter(s => siteEnabled[s.value]).map(s => s.value)
}

async function clearHosts() {
  if (!window.confirm('确定清空插件已写入 hosts 的内容吗？\n将删除管理区块并恢复为原始 hosts。')) return
  try {
    const resp = await props.api?.post(`plugin/${props.pluginId}/clear_hosts`, {});
    if (resp && resp.success === false) {
      throw new Error(resp.message || '清空失败')
    }
    if (toast && typeof toast.success === 'function') {
      toast.success(resp?.message || '已清空写入');
    }
    if (resp && resp.success) viewHosts();
  } catch (e) {
    console.error('清空写入失败', e);
    if (toast && typeof toast.error === 'function') {
      toast.error('清空失败：' + (e?.message || e));
    } else {
      window.alert('清空失败：' + (e?.message || e));
    }
  }
}

async function viewHosts() {
  viewDialog.value = true;
  viewLoading.value = true;
  viewBlock.value = '';
  viewMsg.value = '';
  viewLogs.value = [];
  try {
    const data = await props.api?.get(`plugin/${props.pluginId}/view_hosts`);
    viewMsg.value = data?.message || '';
    viewBlock.value = data?.block || '';
    viewLogs.value = Array.isArray(data?.logs) ? data.logs : [];
  } catch (e) {
    console.error('查看 hosts 失败', e);
    viewMsg.value = '读取失败：' + (e?.message || e);
  } finally {
    viewLoading.value = false;
  }
}

async function save() {
  saving.value = true;
  const willRun = runNowToggle.value;
  try {
    const resp = await props.api?.post(`plugin/${props.pluginId}/save_config`, {
      enabled: cfg.enabled,
      notify: cfg.notify,
      cron: cfg.cron,
      sites: selectedSites(),
      custom_sites: cfg.custom_sites,
      cfst_mirror: cfg.cfst_mirror,
      delay_limit: cfg.delay_limit,
      download_num: cfg.download_num,
      download_limit: cfg.download_limit,
      retry_count: cfg.retry_count,
      retry_interval: cfg.retry_interval,
      overwrite_mode: cfg.overwrite_mode,
      run_now: willRun,
    });
    if (resp && resp.success === false) {
      throw new Error(resp.message || '保存失败')
    }
    if (willRun) {
      runNowToggle.value = false;
      if (resp && resp.background) {
        if (toast && typeof toast.success === 'function') {
          toast.success('已开始运行（后台执行中）');
        }
      } else {
        if (toast && typeof toast.success === 'function') {
          toast.success(resp?.message || '设置已保存');
        }
      }
    } else if (toast && typeof toast.success === 'function') {
      toast.success('设置已保存');
    }
    emit('action');
    window.dispatchEvent(new KeyboardEvent('keydown', {
      key: 'Escape', code: 'Escape', bubbles: true, cancelable: true,
    }));
  } catch (e) {
    console.error('保存失败', e);
    if (toast && typeof toast.error === 'function') {
      toast.error('保存失败：' + (e?.message || e));
    } else {
      window.alert('保存失败：' + (e?.message || e));
    }
  } finally {
    saving.value = false;
  }
}

onMounted(load);

return (_ctx, _cache) => {
  const _component_v_icon = _resolveComponent("v-icon");
  const _component_v_switch = _resolveComponent("v-switch");
  const _component_v_card_text = _resolveComponent("v-card-text");
  const _component_v_card = _resolveComponent("v-card");
  const _component_v_text_field = _resolveComponent("v-text-field");
  const _component_v_btn = _resolveComponent("v-btn");
  const _component_v_select = _resolveComponent("v-select");
  const _component_v_col = _resolveComponent("v-col");
  const _component_v_row = _resolveComponent("v-row");
  const _component_v_chip = _resolveComponent("v-chip");
  const _component_v_expansion_panel_title = _resolveComponent("v-expansion-panel-title");
  const _component_v_checkbox = _resolveComponent("v-checkbox");
  const _component_v_expansion_panel_text = _resolveComponent("v-expansion-panel-text");
  const _component_v_expansion_panel = _resolveComponent("v-expansion-panel");
  const _component_v_expansion_panels = _resolveComponent("v-expansion-panels");
  const _component_v_textarea = _resolveComponent("v-textarea");
  const _component_v_alert = _resolveComponent("v-alert");
  const _component_v_card_title = _resolveComponent("v-card-title");
  const _component_v_spacer = _resolveComponent("v-spacer");
  const _component_v_card_actions = _resolveComponent("v-card-actions");
  const _component_v_dialog = _resolveComponent("v-dialog");

  return (_openBlock(), _createElementBlock("div", _hoisted_1, [
    _createVNode(_component_v_card, {
      variant: "tonal",
      flat: "",
      class: "yh-state-card mb-3",
      style: {"border-radius":"10px"}
    }, {
      default: _withCtx(() => [
        _createVNode(_component_v_card_text, { style: {"padding":"4px 8px"} }, {
          default: _withCtx(() => [
            _createElementVNode("div", _hoisted_2, [
              _createElementVNode("div", {
                class: _normalizeClass(["yh-state-item", { 'yh-state-off': !cfg.enabled }]),
                style: {"flex":"1 1 0","display":"flex","align-items":"center","justify-content":"center","gap":"6px","cursor":"pointer","user-select":"none","padding":"8px 4px","border-radius":"8px"},
                onClick: _cache[0] || (_cache[0] = $event => (cfg.enabled = !cfg.enabled)),
                title: "点击整块切换启用状态"
              }, [
                _createVNode(_component_v_icon, {
                  size: "18",
                  color: cfg.enabled ? 'primary' : ''
                }, {
                  default: _withCtx(() => [...(_cache[25] || (_cache[25] = [
                    _createTextVNode("mdi-lightning-bolt", -1)
                  ]))]),
                  _: 1
                }, 8, ["color"]),
                _cache[26] || (_cache[26] = _createElementVNode("span", { style: {"font-size":"14px","font-weight":"600"} }, "启用插件", -1)),
                _createVNode(_component_v_switch, {
                  "model-value": cfg.enabled,
                  density: "compact",
                  "hide-details": "",
                  color: "primary",
                  inset: "",
                  style: {"margin":"0","pointer-events":"none"}
                }, null, 8, ["model-value"])
              ], 2),
              _cache[31] || (_cache[31] = _createElementVNode("div", { style: {"width":"1px","height":"32px","background":"rgba(128,128,128,0.25)","align-self":"center"} }, null, -1)),
              _createElementVNode("div", {
                class: _normalizeClass(["yh-state-item", { 'yh-state-off': !cfg.notify }]),
                style: {"flex":"1 1 0","display":"flex","align-items":"center","justify-content":"center","gap":"6px","cursor":"pointer","user-select":"none","padding":"8px 4px","border-radius":"8px"},
                onClick: _cache[1] || (_cache[1] = $event => (cfg.notify = !cfg.notify)),
                title: "点击整块切换通知开关"
              }, [
                _createVNode(_component_v_icon, {
                  size: "18",
                  color: cfg.notify ? 'primary' : ''
                }, {
                  default: _withCtx(() => [...(_cache[27] || (_cache[27] = [
                    _createTextVNode("mdi-bell-outline", -1)
                  ]))]),
                  _: 1
                }, 8, ["color"]),
                _cache[28] || (_cache[28] = _createElementVNode("span", { style: {"font-size":"14px","font-weight":"600"} }, "发送通知", -1)),
                _createVNode(_component_v_switch, {
                  "model-value": cfg.notify,
                  density: "compact",
                  "hide-details": "",
                  color: "primary",
                  inset: "",
                  style: {"margin":"0","pointer-events":"none"}
                }, null, 8, ["model-value"])
              ], 2),
              _cache[32] || (_cache[32] = _createElementVNode("div", { style: {"width":"1px","height":"32px","background":"rgba(128,128,128,0.25)","align-self":"center"} }, null, -1)),
              _createElementVNode("div", {
                class: _normalizeClass(["yh-state-item", { 'yh-state-off': !cfg.enabled }]),
                style: {"flex":"1 1 0","display":"flex","align-items":"center","justify-content":"center","gap":"6px","cursor":"pointer","user-select":"none","padding":"8px 4px","border-radius":"8px"},
                title: cfg.enabled ? '开启后点击保存设置，即立即运行一次全局优选' : '插件未启用，无法立即运行',
                onClick: _cache[2] || (_cache[2] = $event => (runNowToggle.value = !runNowToggle.value))
              }, [
                _createVNode(_component_v_icon, {
                  size: "18",
                  color: cfg.enabled && runNowToggle.value ? 'primary' : ''
                }, {
                  default: _withCtx(() => [...(_cache[29] || (_cache[29] = [
                    _createTextVNode("mdi-play-circle-outline", -1)
                  ]))]),
                  _: 1
                }, 8, ["color"]),
                _cache[30] || (_cache[30] = _createElementVNode("span", { style: {"font-size":"14px","font-weight":"600"} }, "立即运行", -1)),
                _createVNode(_component_v_switch, {
                  "model-value": runNowToggle.value,
                  density: "compact",
                  "hide-details": "",
                  color: "primary",
                  inset: "",
                  style: {"margin":"0","pointer-events":"none"}
                }, null, 8, ["model-value"])
              ], 10, _hoisted_3)
            ])
          ]),
          _: 1
        })
      ]),
      _: 1
    }),
    _createVNode(_component_v_row, { class: "mb-2" }, {
      default: _withCtx(() => [
        _createVNode(_component_v_col, { cols: "12" }, {
          default: _withCtx(() => [
            _createVNode(_component_v_text_field, {
              modelValue: cfg.cron,
              "onUpdate:modelValue": _cache[3] || (_cache[3] = $event => ((cfg.cron) = $event)),
              label: "定时运行频率",
              placeholder: "如 0 4 * * *",
              density: "compact",
              "hide-details": "",
              onClick: _cache[4] || (_cache[4] = $event => (cronOpen.value = !cronOpen.value))
            }, null, 8, ["modelValue"]),
            (cronOpen.value)
              ? (_openBlock(), _createElementBlock("div", _hoisted_4, [
                  _createElementVNode("div", _hoisted_5, [
                    _cache[34] || (_cache[34] = _createElementVNode("span", { style: {"font-size":"12px","color":"rgba(128,128,128,0.9)","line-height":"28px"} }, "快捷：", -1)),
                    (_openBlock(), _createElementBlock(_Fragment, null, _renderList(cronPresets, (p) => {
                      return _createVNode(_component_v_btn, {
                        key: p.key,
                        size: "x-small",
                        variant: "tonal",
                        color: "primary",
                        class: "yh-cron-btn",
                        onClick: $event => (cfg.cron = p.cron)
                      }, {
                        default: _withCtx(() => [
                          _createTextVNode(_toDisplayString(p.label), 1)
                        ]),
                        _: 2
                      }, 1032, ["onClick"])
                    }), 64)),
                    _createVNode(_component_v_btn, {
                      size: "x-small",
                      variant: "tonal",
                      class: "yh-cron-btn",
                      color: cronCustom.value ? 'primary' : '',
                      onClick: _cache[5] || (_cache[5] = $event => (cronCustom.value = !cronCustom.value))
                    }, {
                      default: _withCtx(() => [
                        _createTextVNode(_toDisplayString(cronCustom.value ? '收起自定义' : '自定义'), 1)
                      ]),
                      _: 1
                    }, 8, ["color"]),
                    _createVNode(_component_v_btn, {
                      size: "x-small",
                      variant: "tonal",
                      class: "yh-cron-btn",
                      color: !cfg.cron ? 'error' : '',
                      title: "不执行定时运行",
                      onClick: _cache[6] || (_cache[6] = $event => (cfg.cron = ''))
                    }, {
                      default: _withCtx(() => [...(_cache[33] || (_cache[33] = [
                        _createTextVNode("关闭", -1)
                      ]))]),
                      _: 1
                    }, 8, ["color"])
                  ]),
                  (cronCustom.value)
                    ? (_openBlock(), _createElementBlock("div", _hoisted_6, [
                        _createElementVNode("div", _hoisted_7, [
                          _cache[36] || (_cache[36] = _createElementVNode("span", { style: {"font-size":"12px","color":"rgba(128,128,128,0.9)"} }, "分", -1)),
                          _createVNode(_component_v_select, {
                            "model-value": cronParts.min,
                            density: "compact",
                            "hide-details": "",
                            items: cronFieldMin,
                            style: {"max-width":"96px"},
                            "onUpdate:modelValue": _cache[7] || (_cache[7] = $event => (cronParts.min = $event))
                          }, null, 8, ["model-value"]),
                          _cache[37] || (_cache[37] = _createElementVNode("span", { style: {"font-size":"12px","color":"rgba(128,128,128,0.9)"} }, "时", -1)),
                          _createVNode(_component_v_select, {
                            "model-value": cronParts.hour,
                            density: "compact",
                            "hide-details": "",
                            items: cronFieldHour,
                            style: {"max-width":"96px"},
                            "onUpdate:modelValue": _cache[8] || (_cache[8] = $event => (cronParts.hour = $event))
                          }, null, 8, ["model-value"]),
                          _cache[38] || (_cache[38] = _createElementVNode("span", { style: {"font-size":"12px","color":"rgba(128,128,128,0.9)"} }, "日", -1)),
                          _createVNode(_component_v_select, {
                            "model-value": cronParts.day,
                            density: "compact",
                            "hide-details": "",
                            items: cronFieldDay,
                            style: {"max-width":"96px"},
                            "onUpdate:modelValue": _cache[9] || (_cache[9] = $event => (cronParts.day = $event))
                          }, null, 8, ["model-value"]),
                          _cache[39] || (_cache[39] = _createElementVNode("span", { style: {"font-size":"12px","color":"rgba(128,128,128,0.9)"} }, "月", -1)),
                          _createVNode(_component_v_select, {
                            "model-value": cronParts.month,
                            density: "compact",
                            "hide-details": "",
                            items: cronFieldMonth,
                            style: {"max-width":"96px"},
                            "onUpdate:modelValue": _cache[10] || (_cache[10] = $event => (cronParts.month = $event))
                          }, null, 8, ["model-value"]),
                          _cache[40] || (_cache[40] = _createElementVNode("span", { style: {"font-size":"12px","color":"rgba(128,128,128,0.9)"} }, "周", -1)),
                          _createVNode(_component_v_select, {
                            "model-value": cronParts.week,
                            density: "compact",
                            "hide-details": "",
                            items: cronFieldWeek,
                            style: {"max-width":"96px"},
                            "onUpdate:modelValue": _cache[11] || (_cache[11] = $event => (cronParts.week = $event))
                          }, null, 8, ["model-value"]),
                          _createVNode(_component_v_btn, {
                            size: "small",
                            variant: "tonal",
                            density: "compact",
                            color: "primary",
                            style: {"min-width":"64px"},
                            onClick: applyCronCustom
                          }, {
                            default: _withCtx(() => [...(_cache[35] || (_cache[35] = [
                              _createTextVNode("应用", -1)
                            ]))]),
                            _: 1
                          })
                        ]),
                        _cache[41] || (_cache[41] = _createElementVNode("div", { style: {"font-size":"12px","color":"rgba(128,128,128,0.85)","margin-top":"8px"} }, " * 表示任意；分/时/日/月/周对应 cron 五级 ", -1))
                      ]))
                    : _createCommentVNode("", true)
                ]))
              : _createCommentVNode("", true)
          ]),
          _: 1
        })
      ]),
      _: 1
    }),
    _createVNode(_component_v_expansion_panels, {
      modelValue: sitePanel.value,
      "onUpdate:modelValue": _cache[12] || (_cache[12] = $event => ((sitePanel).value = $event)),
      variant: "accordion",
      density: "compact",
      class: "mt-1 mb-2 yh-panel",
      style: {"border-radius":"10px"}
    }, {
      default: _withCtx(() => [
        _createVNode(_component_v_expansion_panel, { style: {"background":"transparent"} }, {
          default: _withCtx(() => [
            _createVNode(_component_v_expansion_panel_title, { style: {"padding":"8px 14px"} }, {
              default: _withCtx(() => [
                _createElementVNode("div", _hoisted_8, [
                  _createElementVNode("div", _hoisted_9, [
                    _createVNode(_component_v_icon, {
                      size: "18",
                      color: "primary"
                    }, {
                      default: _withCtx(() => [...(_cache[42] || (_cache[42] = [
                        _createTextVNode("mdi-web", -1)
                      ]))]),
                      _: 1
                    }),
                    _createElementVNode("span", _hoisted_10, "目标站点（已选 " + _toDisplayString(selectedCount.value) + "/" + _toDisplayString(siteItems.value.length) + "）", 1)
                  ]),
                  (selectedCount.value > 0)
                    ? (_openBlock(), _createElementBlock("div", _hoisted_11, [
                        (_openBlock(true), _createElementBlock(_Fragment, null, _renderList(selectedSiteNames.value, (s) => {
                          return (_openBlock(), _createBlock(_component_v_chip, {
                            key: s,
                            size: "x-small",
                            density: "compact",
                            variant: "tonal",
                            color: "primary"
                          }, {
                            default: _withCtx(() => [
                              _createTextVNode(_toDisplayString(s), 1)
                            ]),
                            _: 2
                          }, 1024))
                        }), 128))
                      ]))
                    : (_openBlock(), _createElementBlock("div", _hoisted_12, " 未选择站点（全局优选后不会写入任何站点） "))
                ])
              ]),
              _: 1
            }),
            _createVNode(_component_v_expansion_panel_text, null, {
              default: _withCtx(() => [
                _createElementVNode("div", _hoisted_13, [
                  _createElementVNode("div", _hoisted_14, [
                    _cache[45] || (_cache[45] = _createElementVNode("span", { style: {"font-size":"12px","color":"rgba(128,128,128,0.85)","flex":"1 1 auto"} }, " 全局优选后写入hosts的站点 ", -1)),
                    _createVNode(_component_v_btn, {
                      size: "small",
                      variant: "tonal",
                      density: "compact",
                      color: "primary",
                      style: {"min-width":"64px","flex-shrink":"0"},
                      onClick: selectAllSites
                    }, {
                      default: _withCtx(() => [...(_cache[43] || (_cache[43] = [
                        _createTextVNode("全选", -1)
                      ]))]),
                      _: 1
                    }),
                    _createVNode(_component_v_btn, {
                      size: "small",
                      variant: "tonal",
                      density: "compact",
                      style: {"min-width":"64px","flex-shrink":"0"},
                      onClick: invertSites
                    }, {
                      default: _withCtx(() => [...(_cache[44] || (_cache[44] = [
                        _createTextVNode("反选", -1)
                      ]))]),
                      _: 1
                    })
                  ]),
                  _createVNode(_component_v_row, { dense: "" }, {
                    default: _withCtx(() => [
                      (_openBlock(true), _createElementBlock(_Fragment, null, _renderList(siteItems.value, (s) => {
                        return (_openBlock(), _createBlock(_component_v_col, {
                          key: s.value,
                          cols: "4",
                          md: "2",
                          style: {"display":"flex","align-items":"center"}
                        }, {
                          default: _withCtx(() => [
                            _createVNode(_component_v_checkbox, {
                              modelValue: siteEnabled[s.value],
                              "onUpdate:modelValue": $event => ((siteEnabled[s.value]) = $event),
                              density: "compact",
                              "hide-details": "",
                              label: s.value,
                              title: s.value,
                              class: "yh-site"
                            }, null, 8, ["modelValue", "onUpdate:modelValue", "label", "title"])
                          ]),
                          _: 2
                        }, 1024))
                      }), 128))
                    ]),
                    _: 1
                  })
                ])
              ]),
              _: 1
            })
          ]),
          _: 1
        })
      ]),
      _: 1
    }, 8, ["modelValue"]),
    _createVNode(_component_v_card, {
      variant: "tonal",
      flat: "",
      class: "yh-sub-card mb-2",
      style: {"border-radius":"10px"}
    }, {
      default: _withCtx(() => [
        _createVNode(_component_v_card_text, { style: {"padding":"10px 14px 12px 14px"} }, {
          default: _withCtx(() => [
            _createElementVNode("div", _hoisted_15, [
              _createVNode(_component_v_icon, {
                size: "17",
                color: "primary"
              }, {
                default: _withCtx(() => [...(_cache[46] || (_cache[46] = [
                  _createTextVNode("mdi-pencil-plus-outline", -1)
                ]))]),
                _: 1
              }),
              _cache[47] || (_cache[47] = _createElementVNode("span", { style: {"font-size":"14px","font-weight":"600"} }, "自定义站点", -1))
            ]),
            _createVNode(_component_v_textarea, {
              modelValue: cfg.custom_sites,
              "onUpdate:modelValue": _cache[13] || (_cache[13] = $event => ((cfg.custom_sites) = $event)),
              label: "每行一个：IP 域名",
              placeholder: "如：104.16.0.1 example.com（每行一个 IP + 域名）",
              rows: "3",
              "auto-grow": "",
              density: "compact",
              "hide-details": ""
            }, null, 8, ["modelValue"]),
            (_openBlock(true), _createElementBlock(_Fragment, null, _renderList(conflictMsgs.value, (msg, i) => {
              return (_openBlock(), _createElementBlock("div", {
                key: i,
                style: {"font-size":"12px","color":"#c62828","padding":"4px 0 0 0"}
              }, _toDisplayString(msg), 1))
            }), 128)),
            (conflictMsgs.value.length === 0)
              ? (_openBlock(), _createElementBlock("div", _hoisted_16, " 自定义站点为「IP 域名」显式指定，直接写入，不参与测速优选；域名与目标站点重复时保存会提示冲突。 "))
              : _createCommentVNode("", true)
          ]),
          _: 1
        })
      ]),
      _: 1
    }),
    _createVNode(_component_v_expansion_panels, {
      modelValue: advPanel.value,
      "onUpdate:modelValue": _cache[21] || (_cache[21] = $event => ((advPanel).value = $event)),
      variant: "accordion",
      density: "compact",
      class: "mt-1 mb-2 yh-panel",
      style: {"border-radius":"10px"}
    }, {
      default: _withCtx(() => [
        _createVNode(_component_v_expansion_panel, { style: {"background":"transparent"} }, {
          default: _withCtx(() => [
            _createVNode(_component_v_expansion_panel_title, { style: {"padding":"10px 14px"} }, {
              default: _withCtx(() => [
                _createElementVNode("div", _hoisted_17, [
                  _createVNode(_component_v_icon, {
                    size: "18",
                    color: "primary"
                  }, {
                    default: _withCtx(() => [...(_cache[48] || (_cache[48] = [
                      _createTextVNode("mdi-tune-variant", -1)
                    ]))]),
                    _: 1
                  }),
                  _cache[49] || (_cache[49] = _createElementVNode("span", { style: {"font-size":"14px","font-weight":"600"} }, "高级设置", -1))
                ])
              ]),
              _: 1
            }),
            _createVNode(_component_v_expansion_panel_text, null, {
              default: _withCtx(() => [
                _createVNode(_component_v_row, { dense: "" }, {
                  default: _withCtx(() => [
                    _createVNode(_component_v_col, {
                      cols: "12",
                      md: "3"
                    }, {
                      default: _withCtx(() => [
                        _createVNode(_component_v_text_field, {
                          modelValue: cfg.cfst_mirror,
                          "onUpdate:modelValue": _cache[14] || (_cache[14] = $event => ((cfg.cfst_mirror) = $event)),
                          label: "cfst 下载镜像（可空）",
                          placeholder: "如 https://gh-proxy.com/",
                          density: "compact",
                          "hide-details": ""
                        }, null, 8, ["modelValue"])
                      ]),
                      _: 1
                    }),
                    _createVNode(_component_v_col, {
                      cols: "12",
                      md: "3"
                    }, {
                      default: _withCtx(() => [
                        _createVNode(_component_v_text_field, {
                          modelValue: cfg.delay_limit,
                          "onUpdate:modelValue": _cache[15] || (_cache[15] = $event => ((cfg.delay_limit) = $event)),
                          label: "延迟上限（毫秒）",
                          type: "number",
                          density: "compact",
                          "hide-details": ""
                        }, null, 8, ["modelValue"])
                      ]),
                      _: 1
                    }),
                    _createVNode(_component_v_col, {
                      cols: "12",
                      md: "3"
                    }, {
                      default: _withCtx(() => [
                        _createVNode(_component_v_text_field, {
                          modelValue: cfg.download_num,
                          "onUpdate:modelValue": _cache[16] || (_cache[16] = $event => ((cfg.download_num) = $event)),
                          label: "下载测速数量",
                          type: "number",
                          density: "compact",
                          "hide-details": ""
                        }, null, 8, ["modelValue"])
                      ]),
                      _: 1
                    }),
                    _createVNode(_component_v_col, {
                      cols: "12",
                      md: "3"
                    }, {
                      default: _withCtx(() => [
                        _createVNode(_component_v_text_field, {
                          modelValue: cfg.download_limit,
                          "onUpdate:modelValue": _cache[17] || (_cache[17] = $event => ((cfg.download_limit) = $event)),
                          label: "下载速度下限（MB/s，0=不限）",
                          type: "number",
                          density: "compact",
                          "hide-details": ""
                        }, null, 8, ["modelValue"])
                      ]),
                      _: 1
                    }),
                    _createVNode(_component_v_col, {
                      cols: "12",
                      md: "3"
                    }, {
                      default: _withCtx(() => [
                        _createVNode(_component_v_text_field, {
                          modelValue: cfg.retry_count,
                          "onUpdate:modelValue": _cache[18] || (_cache[18] = $event => ((cfg.retry_count) = $event)),
                          label: "未达标重试次数",
                          type: "number",
                          density: "compact",
                          "hide-details": ""
                        }, null, 8, ["modelValue"])
                      ]),
                      _: 1
                    }),
                    _createVNode(_component_v_col, {
                      cols: "12",
                      md: "3"
                    }, {
                      default: _withCtx(() => [
                        _createVNode(_component_v_text_field, {
                          modelValue: cfg.retry_interval,
                          "onUpdate:modelValue": _cache[19] || (_cache[19] = $event => ((cfg.retry_interval) = $event)),
                          label: "重试间隔（秒）",
                          type: "number",
                          density: "compact",
                          "hide-details": ""
                        }, null, 8, ["modelValue"])
                      ]),
                      _: 1
                    }),
                    _createVNode(_component_v_col, {
                      cols: "12",
                      md: "3"
                    }, {
                      default: _withCtx(() => [
                        _createVNode(_component_v_select, {
                          modelValue: cfg.overwrite_mode,
                          "onUpdate:modelValue": _cache[20] || (_cache[20] = $event => ((cfg.overwrite_mode) = $event)),
                          label: "hosts 覆盖逻辑",
                          items: overwriteItems,
                          "item-title": "text",
                          "item-value": "value",
                          density: "compact",
                          "hide-details": ""
                        }, null, 8, ["modelValue"])
                      ]),
                      _: 1
                    }),
                    _createVNode(_component_v_col, {
                      cols: "12",
                      md: "3"
                    }, {
                      default: _withCtx(() => [
                        _createVNode(_component_v_text_field, {
                          label: "预留功能",
                          disabled: "",
                          density: "compact",
                          "hide-details": ""
                        })
                      ]),
                      _: 1
                    }),
                    _createVNode(_component_v_col, { cols: "12" }, {
                      default: _withCtx(() => [
                        _createVNode(_component_v_alert, {
                          type: "info",
                          variant: "tonal",
                          density: "compact"
                        }, {
                          default: _withCtx(() => [...(_cache[50] || (_cache[50] = [
                            _createElementVNode("div", { style: {"font-size":"13px","line-height":"1.8"} }, " cfst 首次运行会自动从 GitHub Releases 下载（失败自动重试 3 次）；国内网络下载失败或过慢时，可填写镜像前缀，插件会自动拼接到官方下载地址前。推荐镜像依次尝试： ", -1),
                            _createElementVNode("div", { style: {"font-size":"12px","color":"rgba(128,128,128,0.9)","line-height":"1.9","margin-top":"4px"} }, [
                              _createTextVNode(" https://gh-proxy.com/；https://ghproxy.net/"),
                              _createElementVNode("br"),
                              _createTextVNode(" （未一一列举，可自行前往 "),
                              _createElementVNode("span", { style: {"color":"#1565c0"} }, "https://xiake.pro/github/"),
                              _createTextVNode(" 获取更多） ")
                            ], -1)
                          ]))]),
                          _: 1
                        })
                      ]),
                      _: 1
                    })
                  ]),
                  _: 1
                })
              ]),
              _: 1
            })
          ]),
          _: 1
        })
      ]),
      _: 1
    }, 8, ["modelValue"]),
    (runResult.value)
      ? (_openBlock(), _createBlock(_component_v_alert, {
          key: 0,
          type: runResult.value.type,
          variant: "tonal",
          density: "compact",
          class: "mt-2",
          dismissible: "",
          "onUpdate:modelValue": _cache[22] || (_cache[22] = $event => (runResult.value = null)),
          text: runResult.value.text
        }, null, 8, ["type", "text"]))
      : _createCommentVNode("", true),
    _createElementVNode("div", _hoisted_18, [
      _createVNode(_component_v_alert, {
        type: "success",
        variant: "tonal",
        density: "compact",
        text: lastResultOneLine.value || '最近运行：尚未运行',
        style: {"flex":"1 1 260px","min-width":"0","margin":"0"}
      }, null, 8, ["text"]),
      _createElementVNode("div", _hoisted_19, [
        _createVNode(_component_v_btn, {
          color: "primary",
          onClick: viewHosts
        }, {
          default: _withCtx(() => [...(_cache[51] || (_cache[51] = [
            _createTextVNode("写入查看", -1)
          ]))]),
          _: 1
        }),
        _createVNode(_component_v_btn, {
          color: "primary",
          loading: saving.value,
          onClick: save
        }, {
          default: _withCtx(() => [...(_cache[52] || (_cache[52] = [
            _createTextVNode("保存设置", -1)
          ]))]),
          _: 1
        }, 8, ["loading"])
      ])
    ]),
    _createVNode(_component_v_dialog, {
      modelValue: viewDialog.value,
      "onUpdate:modelValue": _cache[24] || (_cache[24] = $event => ((viewDialog).value = $event)),
      width: "620"
    }, {
      default: _withCtx(() => [
        _createVNode(_component_v_card, null, {
          default: _withCtx(() => [
            _createVNode(_component_v_card_title, { style: {"font-size":"15px","font-weight":"600","display":"flex","align-items":"center","justify-content":"space-between"} }, {
              default: _withCtx(() => [
                _cache[54] || (_cache[54] = _createElementVNode("span", null, "hosts 写入情况", -1)),
                _createVNode(_component_v_btn, {
                  size: "small",
                  color: "error",
                  variant: "tonal",
                  onClick: clearHosts
                }, {
                  default: _withCtx(() => [...(_cache[53] || (_cache[53] = [
                    _createTextVNode("清空写入", -1)
                  ]))]),
                  _: 1
                })
              ]),
              _: 1
            }),
            _createVNode(_component_v_card_text, null, {
              default: _withCtx(() => [
                (viewLoading.value)
                  ? (_openBlock(), _createElementBlock("div", _hoisted_20, "读取中…"))
                  : (_openBlock(), _createElementBlock(_Fragment, { key: 1 }, [
                      (groupedViewLogs.value.length)
                        ? (_openBlock(), _createElementBlock("div", _hoisted_21, [
                            (_openBlock(true), _createElementBlock(_Fragment, null, _renderList(groupedViewLogs.value, (g, i) => {
                              return (_openBlock(), _createElementBlock("div", {
                                key: i,
                                style: {"padding":"8px 2px","border-bottom":"1px solid rgba(128,128,128,0.12)","font-size":"13px","line-height":"1.9"}
                              }, [
                                _createElementVNode("div", null, [
                                  _cache[55] || (_cache[55] = _createElementVNode("span", { style: {"font-weight":"600"} }, "IP：", -1)),
                                  _createElementVNode("span", _hoisted_22, _toDisplayString(g.ip), 1)
                                ]),
                                _createElementVNode("div", null, [
                                  _cache[56] || (_cache[56] = _createElementVNode("span", { style: {"font-weight":"600"} }, "写入站点：", -1)),
                                  _createTextVNode(" " + _toDisplayString(g.sites.join('、')), 1)
                                ])
                              ]))
                            }), 128))
                          ]))
                        : (viewBlock.value)
                          ? (_openBlock(), _createElementBlock("pre", _hoisted_23, _toDisplayString(viewBlock.value), 1))
                          : (_openBlock(), _createBlock(_component_v_alert, {
                              key: 2,
                              type: "info",
                              variant: "tonal",
                              density: "compact",
                              text: viewMsg.value || '尚未写入优选记录'
                            }, null, 8, ["text"])),
                      _cache[57] || (_cache[57] = _createElementVNode("div", { style: {"font-size":"12px","color":"rgba(128,128,128,0.8)","padding-top":"6px"} }, " 以上内容位于 /etc/hosts 的「一键优选hosts」管理区块内。 ", -1))
                    ], 64))
              ]),
              _: 1
            }),
            _createVNode(_component_v_card_actions, null, {
              default: _withCtx(() => [
                _createVNode(_component_v_spacer),
                _createVNode(_component_v_btn, {
                  variant: "tonal",
                  onClick: _cache[23] || (_cache[23] = $event => (viewDialog.value = false))
                }, {
                  default: _withCtx(() => [...(_cache[58] || (_cache[58] = [
                    _createTextVNode("关闭", -1)
                  ]))]),
                  _: 1
                })
              ]),
              _: 1
            })
          ]),
          _: 1
        })
      ]),
      _: 1
    }, 8, ["modelValue"])
  ]))
}
}

};
const Page = /*#__PURE__*/_export_sfc(_sfc_main, [['__scopeId',"data-v-7915fe8d"]]);

export { Page as default };
