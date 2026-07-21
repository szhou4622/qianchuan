/**
 * 规则化停投 · 停投记录（内嵌 rule_regulation.html，无 iframe）
 */
(function () {
    function getPywebviewApi() {
        try {
            if (window.top && window.top.pywebviewAPI) return window.top.pywebviewAPI;
        } catch (e) {}
        try {
            if (window.parent && window.parent !== window && window.parent.pywebviewAPI) {
                return window.parent.pywebviewAPI;
            }
        } catch (e2) {}
        if (window.pywebview && window.pywebview.api) return window.pywebview.api;
        return null;
    }

    var RG_STATUS_ITEMS = [
        { value: '', label: '全部' },
        { value: '1', label: '成功' },
        { value: '-1', label: '失败' },
        { value: '2', label: '跳过' },
    ];
    var RG_STOP_ITEMS = [
        { value: '', label: '全部' },
        { value: 'pause', label: '暂停调控' },
        { value: 'delete', label: '删除任务' },
    ];
    var RG_PAGE_ITEMS = [
        { value: '10', label: '10' },
        { value: '20', label: '20' },
        { value: '50', label: '50' },
        { value: '100', label: '100' },
    ];

    var RG_TRIG_METRIC_LABELS = {
        show_cnt_for_roi2_assist: '调控展示次数',
        click_cnt_for_roi2_assist: '调控点击次数',
        ctr_for_roi2_assist: '调控点击率',
        convert_rate_for_roi2_assist: '调控转化率',
        stat_cost_for_roi2_assist: '调控消耗',
        total_pay_order_count_for_roi2_assist: '调控成交订单数',
        total_pay_order_gmv_include_coupon_for_roi2_assist: '调控成交金额',
        total_prepay_and_pay_order_roi2_assist: '调控支付ROI',
        total_cost_per_pay_order_for_roi2_assist: '调控成交订单成本',
        pay_convert_cost_for_roi2_assist: '调控成交成本',
        pay_convert_cnt_for_roi2_assist: '调控成交人数',
        total_order_settle_amount_for_roi2_1h_assist: '调控净成交金额',
        total_refund_order_gmv_for_roi2_1h_rate_assist: '调控1小时内退款率',
        total_prepay_and_pay_settle_roi2_1h_assist: '调控净成交ROI',
        total_pay_order_gmv_for_roi2_assist: '调控用户实际支付金额',
        total_pay_order_coupon_amount_for_roi2_assist: '调控成交智能优惠券金额',
    };

    var RG_TRIG_OP_LABELS = { gt: '大于', gte: '大于或等于', lt: '小于', lte: '小于或等于', eq: '等于' };

    /** 与 dashboard.html ASSIST_METRIC_COLUMNS 一致：任务信息弹窗「指标明细」仅展示调控任务指标 */
    var RG_ASSIST_METRIC_COLS = [
        { key: 'create_time', label: '创建时间', format: 'text' },
        { key: 'updated_at', label: '入库更新时间', format: 'text' },
        { key: 'stat_cost_for_roi2_assist', label: '调控消耗', format: 'yuan' },
        { key: 'total_pay_order_count_for_roi2_assist', label: '调控成交订单数', format: 'int' },
        { key: 'total_pay_order_gmv_include_coupon_for_roi2_assist', label: '调控成交金额', format: 'yuan' },
        { key: 'total_prepay_and_pay_order_roi2_assist', label: '调控支付ROI', format: 'ratio' },
        { key: 'show_cnt_for_roi2_assist', label: '调控展示次数', format: 'int' },
        { key: 'click_cnt_for_roi2_assist', label: '调控点击次数', format: 'int' },
        { key: 'ctr_for_roi2_assist', label: '调控点击率', format: 'percent' },
        { key: 'convert_rate_for_roi2_assist', label: '调控转化率', format: 'percent' },
        { key: 'total_cost_per_pay_order_for_roi2_assist', label: '调控成交订单成本', format: 'yuan' },
        { key: 'pay_convert_cost_for_roi2_assist', label: '调控成交成本', format: 'yuan' },
        { key: 'pay_convert_cnt_for_roi2_assist', label: '调控成交人数', format: 'int' },
        { key: 'total_order_settle_amount_for_roi2_1h_assist', label: '调控净成交金额', format: 'yuan' },
        { key: 'total_refund_order_gmv_for_roi2_1h_rate_assist', label: '调控1小时内退款率', format: 'percent' },
        { key: 'total_prepay_and_pay_settle_roi2_1h_assist', label: '调控净成交ROI', format: 'ratio' },
        { key: 'total_pay_order_gmv_for_roi2_assist', label: '调控用户实际支付金额', format: 'yuan' },
        { key: 'total_pay_order_coupon_amount_for_roi2_assist', label: '调控成交智能优惠券金额', format: 'yuan' },
    ];

    /** 列表「任务名称」默认预览长度，超出部分需点击展开 */
    var RG_TASK_NAME_PREVIEW_LEN = 15;

    var rgPage = 1;
    var rgTotal = 0;
    var rgPageSize = 50;
    var _rgDdBound = false;
    /** 执行详情弹窗打开时缓存的整行数据（底栏跳转触发条件 / 任务详情） */
    var _rgDetailRowCache = null;

    /** 停投流水 step 字段 → 中文阶段名（与 regulation_service / regulation_rule_runner 一致） */
    var RG_PHASE_LABELS = {
        validate: '参数校验',
        build_url: '构建投放页地址',
        browser: '浏览器初始化',
        switch_to_assist_tab: '切换到调控任务 Tab',
        search_assist_task: '搜索调控任务',
        assist_not_found: '未找到调控任务',
        assist_row: '定位任务行',
        pause_btn: '查找暂停按钮',
        delete_btn: '查找删除按钮',
        batch_update_api: '等待暂停接口响应',
        batch_delete_api: '等待删除接口响应',
        done_already_paused: '跳过停投',
        done: '完成',
        exception: '执行异常',
        resolve_ad_id: '解析计划 ID',
    };

    function esc(s) {
        return String(s == null ? '' : s)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;');
    }

    function rgAttr(s) {
        return String(s == null ? '' : s)
            .replace(/&/g, '&amp;')
            .replace(/"/g, '&quot;')
            .replace(/</g, '&lt;');
    }

    function escapeHtml(s) {
        var d = document.createElement('div');
        d.textContent = s;
        return d.innerHTML;
    }

    function fmtVal(v) {
        if (v === null || v === undefined) return '—';
        if (typeof v === 'object') return JSON.stringify(v);
        return String(v);
    }

    function rrCloseAllDropdowns() {
        document.querySelectorAll('.rr-dd.rr-dd-open').forEach(function (wrap) {
            var panel = wrap._rrPanel;
            wrap.classList.remove('rr-dd-open');
            if (panel && !panel.hidden) panel.hidden = true;
        });
    }

    function ensureRrDdGlobalClose() {
        if (_rgDdBound) return;
        _rgDdBound = true;
        document.addEventListener(
            'click',
            function (e) {
                if (e.target.closest && e.target.closest('.rr-dd')) return;
                rrCloseAllDropdowns();
            },
            true
        );
    }

    function createCustomDropdown(items, value, options) {
        var opt = options || {};
        var wrap = document.createElement('div');
        wrap.className =
            'rr-dd ' + (opt.sm ? 'rr-dd--sm ' : '') + (opt.widthClass || '') + (opt.dropUp ? ' rr-dd--dropup' : '');

        var hidden = document.createElement('input');
        hidden.type = 'hidden';
        hidden.value = value;

        var cur = items.find(function (x) {
            return x.value === value;
        }) || items[0];
        var btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'rr-dd-trigger';
        btn.innerHTML =
            '<span class="rr-dd-label">' +
            escapeHtml(cur.label) +
            '</span><i data-lucide="chevron-down" class="rr-dd-chevron w-3.5 h-3.5 shrink-0 opacity-70"></i>';

        var panel = document.createElement('div');
        panel.className = 'rr-dd-panel';
        panel.hidden = true;
        panel.setAttribute('role', 'listbox');

        items.forEach(function (item) {
            var ob = document.createElement('button');
            ob.type = 'button';
            ob.className = 'rr-dd-opt' + (item.value === value ? ' rr-dd-opt--active' : '');
            ob.textContent = item.label;
            ob.addEventListener('click', function (e) {
                e.stopPropagation();
                hidden.value = item.value;
                var lab = btn.querySelector('.rr-dd-label');
                if (lab) lab.textContent = item.label;
                panel.querySelectorAll('.rr-dd-opt').forEach(function (el) {
                    el.classList.toggle('rr-dd-opt--active', el === ob);
                });
                rrCloseAllDropdowns();
                if (typeof opt.onChange === 'function') opt.onChange(item.value);
            });
            panel.appendChild(ob);
        });

        wrap._rrPanel = panel;
        btn.addEventListener('click', function (e) {
            e.stopPropagation();
            ensureRrDdGlobalClose();
            var wasOpen = wrap.classList.contains('rr-dd-open');
            rrCloseAllDropdowns();
            if (wasOpen) return;
            panel.hidden = false;
            wrap.classList.add('rr-dd-open');
            if (typeof lucide !== 'undefined' && lucide.createIcons) lucide.createIcons();
        });

        wrap.appendChild(hidden);
        wrap.appendChild(btn);
        wrap.appendChild(panel);
        return wrap;
    }

    function rgMountFilters() {
        var ms = document.getElementById('rgMountStatus');
        var mm = document.getElementById('rgMountStop');
        if (!ms || !mm) return;
        rrCloseAllDropdowns();
        ms.innerHTML = '';
        mm.innerHTML = '';
        var dd1 = createCustomDropdown(RG_STATUS_ITEMS, '', {
            sm: true,
            widthClass: 'w-full',
            onChange: function () {
                rgPage = 1;
                rgLoadList();
            },
        });
        dd1.querySelector('input[type="hidden"]').id = 'rgRunsFStatus';
        ms.appendChild(dd1);
        var dd2 = createCustomDropdown(RG_STOP_ITEMS, '', {
            sm: true,
            widthClass: 'w-full',
            onChange: function () {
                rgPage = 1;
                rgLoadList();
            },
        });
        dd2.querySelector('input[type="hidden"]').id = 'rgRunsFStop';
        mm.appendChild(dd2);
        if (typeof lucide !== 'undefined' && lucide.createIcons) lucide.createIcons();
    }

    function rgMountPageSize() {
        var mp = document.getElementById('rgMountPageSize');
        if (!mp) return;
        rrCloseAllDropdowns();
        mp.innerHTML = '';
        var dd = createCustomDropdown(RG_PAGE_ITEMS, '50', {
            sm: true,
            widthClass: 'w-full',
            dropUp: true,
            onChange: function () {
                rgPage = 1;
                rgLoadList();
            },
        });
        dd.querySelector('input[type="hidden"]').id = 'rgRunsPageSize';
        mp.appendChild(dd);
        if (typeof lucide !== 'undefined' && lucide.createIcons) lucide.createIcons();
    }

    function rgTotalPages() {
        return Math.max(1, Math.ceil(rgTotal / rgPageSize));
    }

    function rgFmtAssistCell(format, raw) {
        if (raw === null || raw === undefined || raw === '') return '—';
        var n;
        if (format === 'int') {
            n = parseInt(String(raw).replace(/,/g, ''), 10);
            return Number.isFinite(n) ? String(n) : fmtVal(raw);
        }
        if (format === 'yuan' || format === 'ratio') {
            n = parseFloat(String(raw).replace(/,/g, ''));
            if (!Number.isFinite(n)) return fmtVal(raw);
            var s = n % 1 === 0 ? String(Math.round(n)) : n.toFixed(4).replace(/\.?0+$/, '');
            return format === 'yuan' ? s + ' 元' : s;
        }
        if (format === 'percent') {
            n = parseFloat(String(raw).replace(/%/g, ''));
            if (!Number.isFinite(n)) return fmtVal(raw);
            var p = n <= 1 && n >= 0 ? n * 100 : n;
            var ps = p % 1 === 0 ? String(Math.round(p)) : p.toFixed(2).replace(/\.?0+$/, '');
            return ps + '%';
        }
        return fmtVal(raw);
    }

    /** 与 dashboard：deep_external_action=326 或 deep_external_action_name=支付ROI → 支付 ROI 目标列展示 ecp_roi2_goal */
    function rgIsAssistPayRoiRow(ar) {
        if (!ar || typeof ar !== 'object') return false;
        var dea = ar.deep_external_action;
        if (dea != null && String(dea).trim() !== '' && Number(dea) === 326) return true;
        var nm = ar.deep_external_action_name != null ? String(ar.deep_external_action_name).trim() : '';
        return nm === '支付ROI';
    }

    function rgFmtAssistMicroYuan(raw) {
        if (raw === null || raw === undefined || raw === '') return '—';
        var n = parseFloat(String(raw).replace(/,/g, ''));
        if (!Number.isFinite(n)) return '—';
        var yuan = n / 100000;
        var ys =
            yuan % 1 === 0
                ? String(Math.round(yuan))
                : yuan.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
        return ys + ' 元';
    }

    function rgFmtAssistDurationHours(raw) {
        if (raw === null || raw === undefined || raw === '') return '—';
        var n = parseFloat(String(raw));
        if (!Number.isFinite(n)) return '—';
        if (n === 0) return '长期投放';
        var h = n / 3600;
        return h.toFixed(1);
    }

    function rgFmtAssistQcpxMode(raw) {
        if (raw === null || raw === undefined || raw === '') return '—';
        var n = parseInt(String(raw), 10);
        if (n === 1) return '放量追投';
        if (n === 2) return '控成本追投';
        return String(raw);
    }

    /** 任务信息卡：标签 + 已转义展示文案（内容本身为纯文本） */
    function rgAssistTaskInfoField(label, displayPlain) {
        return (
            '<div class="min-w-0">' +
            '<div class="text-[11px] text-slate-500 mb-1">' +
            esc(label) +
            '</div>' +
            '<div class="text-slate-100 text-[13px] break-words leading-relaxed tabular-nums">' +
            esc(displayPlain) +
            '</div></div>'
        );
    }

    function rgTrigMetricLabel(key) {
        var k = String(key == null ? '' : key).trim();
        return RG_TRIG_METRIC_LABELS[k] || k;
    }

    function rgTrigOpLabel(op) {
        var o = String(op == null ? '' : op).trim().toLowerCase();
        return RG_TRIG_OP_LABELS[o] || String(op || '—');
    }

    function rgTrigGroupCombineText(gc) {
        var x = String(gc == null ? '' : gc).trim().toLowerCase();
        if (x === 'or') return '满足下面任意一组条件即可';
        if (x === 'and') return '需要同时满足下面全部组条件';
        return String(gc || '—');
    }

    function rgTrigJoinText(j) {
        var x = String(j == null ? '' : j).trim().toLowerCase();
        if (x === 'and') return '本组内全部条件需同时满足';
        if (x === 'or') return '本组内满足任一条件即可';
        return String(j || '—');
    }

    function rgBuildTriggerConfigSection(cfg) {
        if (!cfg || typeof cfg !== 'object') return '';
        var gcText = rgTrigGroupCombineText(cfg.group_combine);
        var groups = cfg.groups || [];
        var parts = [];
        parts.push('<div class="runs-trig-panel rounded-xl border border-slate-600/45 bg-slate-950/50 overflow-hidden">');
        parts.push(
            '<div class="px-4 py-2.5 border-b border-slate-700/55 bg-slate-900/70"><span class="text-sm font-semibold text-slate-100">触发规则</span></div>'
        );
        parts.push('<div class="p-4 space-y-4 text-sm">');
        parts.push(
            '<p class="text-slate-300 text-[13px] leading-relaxed"><span class="text-slate-500">多组关系：</span>' +
                esc(gcText) +
                '</p>'
        );
        if (!groups.length) {
            parts.push('<p class="text-slate-500 text-[13px]">暂无具体条件</p>');
        }
        groups.forEach(function (g, gi) {
            var joinText = rgTrigJoinText(g.join);
            parts.push('<div class="rounded-lg border border-slate-700/60 bg-slate-900/40 p-3">');
            parts.push(
                '<div class="text-xs font-semibold text-amber-400/95 mb-1">条件组 ' + (gi + 1) + '</div>'
            );
            parts.push('<p class="text-[11px] text-slate-500 mb-2 leading-relaxed">' + esc(joinText) + '</p>');
            parts.push('<ul class="space-y-2">');
            (g.conditions || []).forEach(function (c) {
                var ml = rgTrigMetricLabel(c.metric);
                var ol = rgTrigOpLabel(c.op);
                var vl = esc(fmtVal(c.value));
                parts.push(
                    '<li class="rounded-md border border-slate-700/40 bg-slate-950/60 px-3 py-2.5 text-[13px] leading-snug">' +
                        '<span class="text-slate-100">' +
                        esc(ml) +
                        '</span> <span class="text-slate-500">需要</span> <span class="text-sky-300">' +
                        esc(ol) +
                        '</span> <span class="text-slate-500">阈值</span> <span class="text-white font-semibold tabular-nums">' +
                        vl +
                        '</span></li>'
                );
            });
            parts.push('</ul></div>');
        });
        parts.push('</div></div>');
        return parts.join('');
    }

    function rgBuildTriggerEvaluationSection(ev) {
        if (!ev || typeof ev !== 'object') return '';
        var passed = !!ev.passed;
        var badge = passed
            ? '<span class="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-semibold bg-emerald-500/15 text-emerald-400 border border-emerald-500/35">通过</span>'
            : '<span class="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-semibold bg-rose-500/15 text-rose-400 border border-rose-500/35">未通过</span>';
        var parts = [];
        parts.push(
            '<div class="runs-trig-panel rounded-xl border border-slate-600/45 bg-slate-950/50 overflow-hidden mt-4">'
        );
        parts.push(
            '<div class="px-4 py-2.5 border-b border-slate-700/55 bg-slate-900/70 flex flex-wrap items-center justify-between gap-2">' +
                '<span class="text-sm font-semibold text-slate-100">本次求值结果</span>' +
                badge +
                '</div>'
        );
        parts.push('<div class="p-4 space-y-3">');
        (ev.groups || []).forEach(function (g, gi) {
            var gp = g.passed;
            var st = gp
                ? '<span class="text-emerald-400 text-xs font-medium">本组已满足</span>'
                : '<span class="text-rose-400 text-xs font-medium">本组未满足</span>';
            parts.push('<div class="rounded-lg border border-slate-700/60 bg-slate-900/35 p-3">');
            parts.push(
                '<div class="flex flex-wrap items-center gap-2 mb-2 text-xs"><span class="text-slate-400">条件组 ' +
                    (gi + 1) +
                    '</span>' +
                    st +
                    '</div>'
            );
            parts.push('<ul class="space-y-2">');
            (g.conditions || []).forEach(function (c) {
                var ml = rgTrigMetricLabel(c.metric);
                var ol = rgTrigOpLabel(c.op);
                var ok = c.passed;
                var mark = ok
                    ? '<span class="text-emerald-400 font-semibold">满足</span>'
                    : '<span class="text-rose-400 font-semibold">不满足</span>';
                parts.push(
                    '<li class="rounded-md border border-slate-700/35 bg-slate-950/55 px-3 py-2.5 text-[13px]">' +
                        '<div class="text-slate-100 mb-1.5">' +
                        esc(ml) +
                        '</div>' +
                        '<div class="text-[12px] text-slate-400 flex flex-wrap items-center gap-x-3 gap-y-1 leading-relaxed">' +
                        '<span>要求「<span class="text-slate-300">' +
                        esc(ol) +
                        '」</span> 阈值 <span class="text-slate-200 tabular-nums">' +
                        esc(fmtVal(c.threshold)) +
                        '</span></span>' +
                        '<span>实际 <span class="text-slate-200 tabular-nums">' +
                        esc(fmtVal(c.actual)) +
                        '</span></span>' +
                        '<span class="text-slate-600">·</span>' +
                        mark +
                        '</div></li>'
                );
            });
            parts.push('</ul></div>');
        });
        parts.push('</div></div>');
        return parts.join('');
    }

    function rgRenderTriggerModal(obj, triggerSourceHint) {
        var el = document.getElementById('modalTrigBody');
        if (!el) return;
        if (!obj || typeof obj !== 'object') {
            el.innerHTML = '<p class="text-rose-400">无法解析 JSON</p>';
            return;
        }
        var src = obj.source != null ? String(obj.source).trim().toLowerCase() : '';
        if (!src && triggerSourceHint != null) {
            var th = String(triggerSourceHint).trim().toLowerCase();
            if (th === 'manual') src = 'manual';
        }
        if (src === 'manual') {
            el.innerHTML =
                '<div class="rounded-xl border border-slate-600/45 bg-slate-950/50 px-4 py-5 text-sm text-slate-300 leading-relaxed">' +
                '用户手动停投，无触发条件' +
                '</div>';
            if (typeof lucide !== 'undefined' && lucide.createIcons) lucide.createIcons();
            return;
        }
        var blocks = [];
        var cfg = obj.trigger_config;
        if (cfg) {
            blocks.push(rgBuildTriggerConfigSection(cfg));
        }
        var ev = obj.evaluation;
        if (ev && typeof ev === 'object') {
            blocks.push(rgBuildTriggerEvaluationSection(ev));
        }
        el.innerHTML = blocks.join('') || '<p class="text-slate-500">无数据</p>';
        if (typeof lucide !== 'undefined' && lucide.createIcons) lucide.createIcons();
    }

    function rgRenderQueryModal(obj) {
        var el = document.getElementById('modalQueryBody');
        var elAt = document.getElementById('modalQueryAt');
        if (elAt) elAt.textContent = '';
        if (!el) return;
        if (!obj || typeof obj !== 'object') {
            el.innerHTML = '<p class="text-rose-400">无法解析 JSON</p>';
            return;
        }
        var queryAtRaw = obj.query_at != null && String(obj.query_at).trim() ? String(obj.query_at).trim() : '';
        if (elAt) elAt.textContent = queryAtRaw || '—';

        var ar = obj.assist_row;
        var taskSummary = '';
        var metricsTable = '';
        if (ar && typeof ar === 'object') {
            var tid = ar.assist_task_id != null ? esc(String(ar.assist_task_id)) : '<span class="text-slate-600">—</span>';
            var tname =
                ar.task_name != null && String(ar.task_name).trim()
                    ? esc(String(ar.task_name))
                    : '<span class="text-slate-600">—</span>';
            var netRoiStr = rgIsAssistPayRoiRow(ar) ? '—' : rgFmtAssistCell('ratio', ar.ecp_roi2_goal);
            var payRoiStr = !rgIsAssistPayRoiRow(ar) ? '—' : rgFmtAssistCell('ratio', ar.ecp_roi2_goal);
            var taskInfoExtra =
                '<div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4 sm:gap-3 mt-5 pt-5 border-t border-slate-700/40">' +
                rgAssistTaskInfoField('调控预算(元)', rgFmtAssistMicroYuan(ar.budget)) +
                rgAssistTaskInfoField('净成交ROI目标', netRoiStr) +
                rgAssistTaskInfoField('支付ROI目标', payRoiStr) +
                rgAssistTaskInfoField('出价', rgFmtAssistMicroYuan(ar.bid)) +
                rgAssistTaskInfoField('调控时长(小时)', rgFmtAssistDurationHours(ar.daily_delivery_seconds)) +
                rgAssistTaskInfoField('追投方式', rgFmtAssistQcpxMode(ar.qcpx_mode)) +
                '</div>';
            taskSummary =
                '<div class="rounded-xl border border-slate-600/45 bg-gradient-to-b from-slate-900/80 to-slate-950/60 overflow-hidden mb-5">' +
                '<div class="px-4 py-2.5 border-b border-slate-700/50 bg-slate-900/70">' +
                '<span class="text-xs font-semibold text-slate-200 tracking-wide">任务信息</span>' +
                '</div>' +
                '<div class="p-4 sm:p-5 text-sm">' +
                '<div class="grid grid-cols-1 sm:grid-cols-3 gap-4 sm:gap-3 sm:items-start">' +
                '<div class="min-w-0">' +
                '<div class="text-[11px] text-slate-500 mb-1">调控任务 ID</div>' +
                '<div class="text-slate-100 font-mono text-[13px] break-all leading-relaxed">' +
                tid +
                '</div></div>' +
                '<div class="min-w-0 sm:col-span-2">' +
                '<div class="text-[11px] text-slate-500 mb-1">任务名称</div>' +
                '<div class="text-slate-100 break-words leading-relaxed">' +
                tname +
                '</div></div>' +
                '</div>' +
                taskInfoExtra +
                '</div></div>';

            var metricRows = RG_ASSIST_METRIC_COLS.map(function (col) {
                var val = ar[col.key];
                var display = rgFmtAssistCell(col.format, val);
                return (
                    '<tr class="border-b border-slate-800/70 last:border-0">' +
                    '<td class="py-2.5 pr-4 text-slate-400 text-[13px] whitespace-nowrap align-top w-[40%] sm:w-[38%]">' +
                    esc(col.label) +
                    '</td>' +
                    '<td class="py-2.5 text-slate-100 text-[13px] break-all align-top tabular-nums">' +
                    esc(display) +
                    '</td></tr>'
                );
            }).join('');
            metricsTable =
                '<div class="rounded-xl border border-slate-700/50 bg-slate-950/35 overflow-hidden">' +
                '<div class="px-4 py-2.5 border-b border-slate-700/50 bg-slate-900/55">' +
                '<span class="text-xs font-semibold text-slate-200">指标明细</span>' +
                '</div>' +
                '<div class="px-2 sm:px-4 py-2 overflow-x-auto">' +
                '<table class="w-full min-w-[280px] border-collapse">' +
                metricRows +
                '</table></div></div>';
        } else {
            taskSummary =
                '<div class="rounded-xl border border-dashed border-slate-700/60 bg-slate-950/30 px-4 py-6 text-center text-sm text-slate-500 mb-5">暂无调控任务行（assist_row）</div>';
        }

        el.innerHTML = taskSummary + metricsTable;
        if (typeof lucide !== 'undefined' && lucide.createIcons) lucide.createIcons();
    }

    function rgPhaseLabelText(raw) {
        var s = raw == null ? '' : String(raw).trim();
        if (!s) return '';
        var k = s.toLowerCase();
        if (RG_PHASE_LABELS[k]) return RG_PHASE_LABELS[k];
        return '未识别（' + s + '）';
    }

    function rgDetailKvField(label, innerHtml) {
        return (
            '<div class="runs-detail-field">' +
            '<div class="text-sm text-slate-500 mb-1.5">' +
            esc(label) +
            '</div>' +
            '<div class="text-base text-slate-100 break-words leading-relaxed">' +
            innerHtml +
            '</div></div>'
        );
    }

    function rgDetailTextOrDash(s) {
        var t = s != null && String(s).trim() !== '' ? String(s).trim() : '';
        return t ? esc(t) : '<span class="text-slate-600">—</span>';
    }

    function rgBrowserHeadlessRuleText(v) {
        if (v === null || v === undefined || v === '') return '';
        var n = parseInt(String(v), 10);
        if (!isFinite(n)) return String(v);
        if (n === 0) return '关闭（有界面）';
        if (n === 1) return '开启（无头）';
        return String(v);
    }

    function rgFmtDurationMs(raw) {
        if (raw === null || raw === undefined || raw === '') return '—';
        var n = parseInt(String(raw), 10);
        if (!isFinite(n) || n < 0) return esc(String(raw));
        if (n < 1000) return n + ' ms';
        var sec = n / 1000;
        var s = sec % 1 === 0 ? String(Math.round(sec)) : String(Number(sec.toFixed(2))).replace(/\.?0+$/, '');
        return s + ' 秒';
    }

    /** 与 rule_retargeting_runs.html 中 runsFmtTriggerSource 一致：中文主标签 + 英文 meta */
    function rgFmtRegulationTriggerSource(ts) {
        var v = String(ts == null ? '' : ts).trim().toLowerCase();
        if (!v || v === 'scheduler') {
            return (
                '<span class="runs-detail-tri">自动停投 <span class="runs-detail-tri-meta">(scheduler)</span></span>'
            );
        }
        if (v === 'manual') {
            return (
                '<span class="runs-detail-tri">手动停投 <span class="runs-detail-tri-meta">(manual)</span></span>'
            );
        }
        return '<span class="runs-detail-tri">' + esc(String(ts)) + '</span>';
    }

    function rgRenderRunDetailModal(d) {
        var el = document.getElementById('modalRunDetailBody');
        if (!el) return;

        var aid = d.assist_task_id != null && String(d.assist_task_id).trim() ? String(d.assist_task_id).trim() : '';
        var idInner = aid
            ? '<button type="button" class="runs-copy runs-copy--modal" data-rg-copy="' +
              rgAttr(aid) +
              '" data-rg-toast="已复制">' +
              esc(aid) +
              '</button>'
            : '<span class="text-slate-600">—</span>';

        var st = parseInt(String(d.status), 10);
        var detailStr = d.detail != null && String(d.detail).trim() ? String(d.detail).trim() : '';
        var showErrBanner = st === -1 && detailStr.length > 0;

        var phaseStr = rgPhaseLabelText(d.step);
        var phaseInner = phaseStr ? esc(phaseStr) : '<span class="text-slate-600">—</span>';

        var statusInner = rgDetailStatusTagHtml(d.status);

        var stopInner = rgDetailStopTagHtml(d.stop_action);

        var bhTxt = rgBrowserHeadlessRuleText(d.browser_headless_rule);
        var bhInner = bhTxt ? esc(bhTxt) : rgDetailTextOrDash(d.browser_headless_rule);

        var parts = [];
        parts.push('<div class="runs-detail-main">');
        parts.push('<div class="runs-detail-param-h text-slate-200">流水字段</div>');
        parts.push('<div class="runs-detail-kv-grid">');
        parts.push(rgDetailKvField('调控任务 ID', idInner));
        parts.push(rgDetailKvField('任务名称', rgDetailTextOrDash(d.task_name)));
        parts.push(rgDetailKvField('策略名称', rgDetailTextOrDash(d.strategy_name)));
        parts.push(rgDetailKvField('停投方式', stopInner));
        parts.push(rgDetailKvField('开始时间', rgDetailTextOrDash(d.started_at)));
        parts.push(rgDetailKvField('结束时间', rgDetailTextOrDash(d.ended_at)));
        parts.push(rgDetailKvField('耗时', esc(rgFmtDurationMs(d.duration_ms))));
        parts.push(rgDetailKvField('状态', statusInner));
        parts.push(rgDetailKvField('阶段', phaseInner));
        parts.push(rgDetailKvField('返回消息', rgDetailTextOrDash(d.message)));
        parts.push(rgDetailKvField('无头浏览器（策略覆盖）', bhInner));
        parts.push(rgDetailKvField('触发来源', rgFmtRegulationTriggerSource(d.trigger_source)));
        parts.push('</div></div>');

        if (showErrBanner) {
            parts.push(
                '<div class="runs-detail-error-banner mt-4">' +
                    '<div class="runs-detail-error-hd"><i data-lucide="alert-triangle" class="w-3.5 h-3.5 shrink-0"></i> 错误详情</div>' +
                    '<div class="runs-detail-error-body font-mono text-sm leading-relaxed">' +
                    esc(detailStr) +
                    '</div></div>'
            );
        }

        el.innerHTML = parts.join('');
        if (typeof lucide !== 'undefined' && lucide.createIcons) lucide.createIcons();
    }

    function rgStrategyNameTagHtml(raw) {
        var t = raw != null && String(raw).trim() !== '' ? String(raw).trim() : '策略';
        return (
            '<span class="inline-flex items-center shrink-0 px-2 py-0.5 rounded-md text-xs font-medium ' +
            'bg-sky-500/15 text-sky-300 border border-sky-500/25">' +
            esc(t) +
            '</span>'
        );
    }

    function rgSetModalTrigStrategyBadge(raw) {
        var el = document.getElementById('modalTrigStrategyBadge');
        if (el) el.innerHTML = rgStrategyNameTagHtml(raw);
    }

    function rgOpenModal(id) {
        var node = document.getElementById(id);
        if (node) {
            node.classList.remove('hidden');
            if (typeof lucide !== 'undefined' && lucide.createIcons) lucide.createIcons();
        }
    }

    function rgCloseModal(id) {
        var node = document.getElementById(id);
        if (node) node.classList.add('hidden');
    }

    function rgShowToast(msg) {
        var toast = document.getElementById('rgRunsToast');
        var tm = document.getElementById('rgRunsToastMessage');
        if (!toast || !tm) return;
        tm.textContent = msg;
        toast.classList.remove('opacity-0');
        toast.classList.add('opacity-100');
        if (typeof lucide !== 'undefined' && lucide.createIcons) lucide.createIcons();
        setTimeout(function () {
            toast.classList.remove('opacity-100');
            toast.classList.add('opacity-0');
        }, 2000);
    }

    function rgCopyText(text, msg) {
        var m = msg || '已复制到剪贴板';
        if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(text).then(
                function () {
                    rgShowToast(m);
                },
                function () {
                    rgShowToast('复制失败');
                }
            );
        } else {
            rgShowToast('复制失败');
        }
    }

    function rgFmtStatusCell(st) {
        if (st === 1) return '<span class="runs-st-ok">成功</span>';
        if (st === 2) return '<span class="runs-st-skip">跳过</span>';
        return '<span class="runs-st-err">失败</span>';
    }

    function rgFmtStop(sa) {
        var v = String(sa == null ? '' : sa).trim().toLowerCase();
        if (v === 'pause') {
            return '<span class="inline-flex items-center px-1.5 py-0.5 bg-sky-500/20 text-sky-400 text-[10px] rounded border border-sky-500/30 leading-none">暂停调控</span>';
        }
        if (v === 'delete') {
            return '<span class="inline-flex items-center px-1.5 py-0.5 bg-rose-500/20 text-rose-400 text-[10px] rounded border border-rose-500/30 leading-none">删除任务</span>';
        }
        return '<span class="runs-empty">—</span>';
    }

    /** 执行详情弹窗 · 停投方式 tag（与列表视觉一致，略大） */
    function rgDetailStopTagHtml(sa) {
        var v = String(sa == null ? '' : sa).trim().toLowerCase();
        if (v === 'pause') {
            return (
                '<span class="inline-flex items-center px-2 py-0.5 rounded-md text-sm font-medium bg-sky-500/15 text-sky-300 border border-sky-500/25">' +
                '暂停调控</span>'
            );
        }
        if (v === 'delete') {
            return (
                '<span class="inline-flex items-center px-2 py-0.5 rounded-md text-sm font-medium bg-rose-500/15 text-rose-400 border border-rose-500/30">' +
                '删除任务</span>'
            );
        }
        if (!v) return '<span class="text-slate-600">—</span>';
        return (
            '<span class="inline-flex items-center px-2 py-0.5 rounded-md text-sm font-medium bg-slate-500/12 text-slate-400 border border-slate-500/25">' +
            esc(v) +
            '</span>'
        );
    }

    /** 执行详情弹窗 · 状态 tag */
    function rgDetailStatusTagHtml(st) {
        var n = parseInt(String(st), 10);
        var base =
            'inline-flex items-center px-2 py-0.5 rounded-md text-sm font-medium border ';
        if (n === 1) {
            return (
                '<span class="' +
                base +
                'bg-emerald-500/15 text-emerald-300 border-emerald-500/25">成功</span>'
            );
        }
        if (n === 2) {
            return (
                '<span class="' +
                base +
                'bg-amber-500/15 text-amber-300 border-amber-500/25">跳过</span>'
            );
        }
        if (n === -1) {
            return (
                '<span class="' +
                base +
                'bg-rose-500/15 text-rose-300 border-rose-500/25">失败</span>'
            );
        }
        return rgDetailTextOrDash(st);
    }

    function rgReturnMessageCell(msg) {
        var raw = msg == null ? '' : String(msg).trim();
        if (!raw) return '<td class="p-2 align-top"><span class="runs-empty">—</span></td>';
        var maxLen = 24;
        var truncated = raw.length > maxLen ? raw.slice(0, maxLen) + '...' : raw;
        return (
            '<td class="p-2 align-top max-w-[10rem]"><div class="runs-cell-plain leading-relaxed" title="' +
            esc(raw) +
            '">' +
            esc(truncated) +
            '</div></td>'
        );
    }

    function rgCopyIdCell(raw, toastMsg) {
        var t = raw == null ? '' : String(raw).trim();
        if (!t) return '<td class="p-2"><span class="runs-empty">—</span></td>';
        return (
            '<td class="p-2 max-w-[9rem] truncate align-top">' +
            '<button type="button" class="runs-copy" data-rg-copy="' +
            rgAttr(t) +
            '" data-rg-toast="' +
            rgAttr(toastMsg || '已复制') +
            '">' +
            esc(t) +
            '</button></td>'
        );
    }

    function rgTaskNameCell(name) {
        var full = String(name == null ? '' : name);
        if (!full.trim()) return '<td class="p-2"><span class="runs-empty">—</span></td>';
        var n = RG_TASK_NAME_PREVIEW_LEN;
        var needFold = full.length > n;
        var preview = needFold ? full.slice(0, n) + '...' : full;
        return (
            '<td class="p-2 max-w-[14rem] align-top">' +
            '<button type="button" class="rg-task-name-toggle block w-full max-w-full text-left text-slate-200 text-[11px] leading-snug cursor-pointer bg-transparent border-0 p-0 rounded-sm hover:text-sky-300 focus:outline-none focus-visible:ring-1 focus-visible:ring-sky-500/40" data-rg-full-name="' +
            rgAttr(full) +
            '" title="' +
            rgAttr(full) +
            '">' +
            esc(preview) +
            '</button></td>'
        );
    }

    function rgStepCell(step) {
        var s = step == null ? '' : String(step).trim();
        if (!s) return '<td class="p-2 align-top"><span class="runs-empty">—</span></td>';
        var full = rgPhaseLabelText(s);
        var maxLen = 22;
        var truncated = full.length > maxLen ? full.slice(0, maxLen) + '…' : full;
        return (
            '<td class="p-2 align-top max-w-[12rem]">' +
            '<div class="text-[11px] text-slate-300 leading-snug runs-cell-plain" title="' +
            esc(full) +
            '">' +
            esc(truncated) +
            '</div></td>'
        );
    }

    function rgUpdatePagination() {
        var paginationLinks = document.getElementById('rgRunsPaginationLinks');
        if (!paginationLinks) return;
        if (rgTotal === 0) {
            paginationLinks.innerHTML = '';
            return;
        }
        var totalPages = rgTotalPages();
        var currentPage = rgPage;
        var html = '';
        var btnBase = 'px-2.5 py-1 text-xs rounded transition-colors';
        var numBase = 'px-2.5 py-1 text-xs text-slate-400 hover:text-white hover:bg-slate-700 rounded transition-colors';
        var numCur = 'px-2.5 py-1 text-xs bg-blue-500 text-white rounded font-medium';

        if (currentPage > 1) {
            html +=
                '<button type="button" data-rg-page="' +
                (currentPage - 1) +
                '" class="' +
                btnBase +
                ' text-slate-400 hover:text-white bg-slate-700 hover:bg-slate-600 border border-slate-600" title="上一页">' +
                '<svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 19l-7-7 7-7"/></svg>' +
                '</button>';
        } else {
            html +=
                '<button type="button" class="' +
                btnBase +
                ' text-slate-600 bg-slate-800 border border-slate-700 cursor-not-allowed" disabled>' +
                '<svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 19l-7-7 7-7"/></svg>' +
                '</button>';
        }

        var j;
        if (totalPages <= 7) {
            for (j = 1; j <= totalPages; j++) {
                if (j === currentPage) {
                    html += '<span class="' + numCur + '">' + j + '</span>';
                } else {
                    html += '<button type="button" data-rg-page="' + j + '" class="' + numBase + '">' + j + '</button>';
                }
            }
        } else if (currentPage <= 4) {
            for (j = 1; j <= 5; j++) {
                if (j === currentPage) {
                    html += '<span class="' + numCur + '">' + j + '</span>';
                } else {
                    html += '<button type="button" data-rg-page="' + j + '" class="' + numBase + '">' + j + '</button>';
                }
            }
            html += '<span class="px-1 text-slate-500">...</span>';
            html +=
                '<button type="button" data-rg-page="' +
                totalPages +
                '" class="' +
                numBase +
                '">' +
                totalPages +
                '</button>';
        } else if (currentPage >= totalPages - 3) {
            html += '<button type="button" data-rg-page="1" class="' + numBase + '">1</button>';
            html += '<span class="px-1 text-slate-500">...</span>';
            for (j = totalPages - 4; j <= totalPages; j++) {
                if (j === currentPage) {
                    html += '<span class="' + numCur + '">' + j + '</span>';
                } else {
                    html += '<button type="button" data-rg-page="' + j + '" class="' + numBase + '">' + j + '</button>';
                }
            }
        } else {
            html += '<button type="button" data-rg-page="1" class="' + numBase + '">1</button>';
            html += '<span class="px-1 text-slate-500">...</span>';
            for (j = currentPage - 2; j <= currentPage + 2; j++) {
                if (j === currentPage) {
                    html += '<span class="' + numCur + '">' + j + '</span>';
                } else {
                    html += '<button type="button" data-rg-page="' + j + '" class="' + numBase + '">' + j + '</button>';
                }
            }
            html += '<span class="px-1 text-slate-500">...</span>';
            html +=
                '<button type="button" data-rg-page="' +
                totalPages +
                '" class="' +
                numBase +
                '">' +
                totalPages +
                '</button>';
        }

        if (currentPage < totalPages) {
            html +=
                '<button type="button" data-rg-page="' +
                (currentPage + 1) +
                '" class="' +
                btnBase +
                ' text-slate-400 hover:text-white bg-slate-700 hover:bg-slate-600 border border-slate-600" title="下一页">' +
                '<svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7"/></svg>' +
                '</button>';
        } else {
            html +=
                '<button type="button" class="' +
                btnBase +
                ' text-slate-600 bg-slate-800 border border-slate-700 cursor-not-allowed" disabled>' +
                '<svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7"/></svg>' +
                '</button>';
        }
        paginationLinks.innerHTML = html;
    }

    async function rgLoadList() {
        var api = getPywebviewApi();
        var tbody = document.getElementById('rgRunsTbody');
        var emptyHint = document.getElementById('rgRunsEmpty');
        if (!tbody || !emptyHint) return;
        if (!api || !api.listRegulationRuns) {
            tbody.innerHTML = '';
            var pl0 = document.getElementById('rgRunsPaginationLinks');
            if (pl0) pl0.innerHTML = '';
            emptyHint.classList.remove('hidden');
            emptyHint.textContent = '无法连接本地 API（请在应用内打开本页）';
            return;
        }
        var psEl = document.getElementById('rgRunsPageSize');
        rgPageSize = psEl ? parseInt(psEl.value, 10) || 50 : 50;
        var fq = document.getElementById('rgRunsFQ');
        var q = fq ? fq.value.trim() : '';
        var fst = document.getElementById('rgRunsFStatus');
        var fsp = document.getElementById('rgRunsFStop');
        var st = fst ? fst.value : '';
        var sa = fsp ? fsp.value.trim() : '';

        var res = await api.listRegulationRuns(
            null,
            null,
            q || null,
            sa || null,
            st === '' ? null : st,
            rgPage,
            rgPageSize
        );
        if (!res || !res.success) {
            tbody.innerHTML = '';
            var pl1 = document.getElementById('rgRunsPaginationLinks');
            if (pl1) pl1.innerHTML = '';
            emptyHint.classList.remove('hidden');
            emptyHint.textContent = (res && res.message) ? res.message : '加载失败';
            return;
        }
        rgTotal = res.total || 0;
        var items = res.items || [];
        var pi = document.getElementById('rgRunsPageInfo');
        if (pi) pi.textContent = '共 ' + rgTotal + ' 条';
        var tp = Math.max(1, Math.ceil(rgTotal / rgPageSize));
        if (rgTotal > 0 && rgPage > tp) {
            rgPage = tp;
            return rgLoadList();
        }
        rgUpdatePagination();

        if (!items.length) {
            tbody.innerHTML = '';
            emptyHint.classList.remove('hidden');
            emptyHint.textContent = '暂无数据';
            return;
        }
        emptyHint.classList.add('hidden');

        tbody.innerHTML = items
            .map(function (r) {
                return (
                    '<tr>' +
                    rgCopyIdCell(r.assist_task_id, '已复制') +
                    rgTaskNameCell(r.task_name) +
                    '<td class="p-2">' +
                    rgFmtStatusCell(r.status) +
                    '</td>' +
                    rgStepCell(r.step) +
                    '<td class="p-2">' +
                    rgFmtStop(r.stop_action) +
                    '</td>' +
                    rgReturnMessageCell(r.message) +
                    '<td class="p-2 whitespace-nowrap font-mono text-[11px] text-slate-400">' +
                    esc(r.started_at) +
                    '</td>' +
                    '<td class="p-2 text-center align-top">' +
                    '<div class="inline-flex flex-wrap justify-center items-center gap-1 max-w-[20rem]">' +
                    '<button type="button" class="runs-btn-op runs-btn-op--detail rg-btn-detail" data-id="' +
                    r.id +
                    '">详情</button>' +
                    '<button type="button" class="runs-btn-op runs-btn-op--trig rg-btn-json" data-id="' +
                    r.id +
                    '" data-kind="trig">触发条件</button>' +
                    '<button type="button" class="runs-btn-op runs-btn-op--query rg-btn-json" data-id="' +
                    r.id +
                    '" data-kind="query">任务信息</button>' +
                    '</div></td></tr>'
                );
            })
            .join('');
    }

    window.refreshRegulationRunsList = rgLoadList;

    function rgBindTabs() {
        var btnCfg = document.getElementById('tabBtnRegConfig');
        var btnRuns = document.getElementById('tabBtnRegRuns');
        var paneCfg = document.getElementById('tabPaneRegConfig');
        var paneRuns = document.getElementById('tabPaneRegRuns');
        if (!btnCfg || !btnRuns || !paneCfg || !paneRuns) return;

        function showConfig() {
            paneCfg.classList.remove('hidden');
            paneRuns.classList.add('hidden');
            btnCfg.classList.add('seg-btn-active');
            btnRuns.classList.remove('seg-btn-active');
        }

        function showRuns() {
            paneCfg.classList.add('hidden');
            paneRuns.classList.remove('hidden');
            btnCfg.classList.remove('seg-btn-active');
            btnRuns.classList.add('seg-btn-active');
            rgLoadList();
        }

        btnCfg.addEventListener('click', showConfig);
        btnRuns.addEventListener('click', showRuns);
    }

    function rgBindRunDetailFooter() {
        var root = document.getElementById('modalRunDetail');
        if (!root) return;
        root.addEventListener('click', function (e) {
            var t = e.target.closest && e.target.closest('#rgRunDetailBtnTrig, #rgRunDetailBtnQuery');
            if (!t) return;
            e.preventDefault();
            var d = _rgDetailRowCache;
            if (!d || typeof d !== 'object') {
                if (window.MessageBox && MessageBox.alert) MessageBox.alert('数据已失效，请从列表重新打开详情');
                return;
            }
            try {
                if (t.id === 'rgRunDetailBtnTrig') {
                    rgRenderTriggerModal(JSON.parse(d.trigger_snapshot_json || '{}'), d.trigger_source);
                    rgSetModalTrigStrategyBadge(d.strategy_name);
                    rgCloseModal('modalRunDetail');
                    rgOpenModal('modalTrig');
                } else {
                    rgRenderQueryModal(JSON.parse(d.query_snapshot_json || '{}'));
                    rgCloseModal('modalRunDetail');
                    rgOpenModal('modalQuery');
                }
            } catch (err) {
                if (window.MessageBox && MessageBox.alert) MessageBox.alert('JSON 解析失败: ' + err);
            }
        });
    }

    function rgBindModals() {
        rgBindRunDetailFooter();
        ['modalTrig', 'modalQuery', 'modalRunDetail'].forEach(function (mid) {
            var root = document.getElementById(mid);
            if (!root) return;
            root.querySelectorAll('.modal-close').forEach(function (b) {
                b.addEventListener('click', function () {
                    rgCloseModal(b.getAttribute('data-close'));
                });
            });
            root.addEventListener('click', function (e) {
                if (e.target === root) rgCloseModal(mid);
            });
        });

        document.addEventListener(
            'click',
            function (e) {
                var copyBtn = e.target.closest && e.target.closest('[data-rg-copy]');
                if (!copyBtn) return;
                e.preventDefault();
                e.stopPropagation();
                var txt = copyBtn.getAttribute('data-rg-copy');
                if (!txt) return;
                var toast = copyBtn.getAttribute('data-rg-toast') || '已复制';
                rgCopyText(txt, toast);
            },
            true
        );

        var tbody = document.getElementById('rgRunsTbody');
        if (tbody) {
            tbody.addEventListener('click', async function (e) {
                var tn = e.target.closest && e.target.closest('.rg-task-name-toggle');
                if (tn) {
                    e.preventDefault();
                    var fullStr = tn.getAttribute('data-rg-full-name');
                    if (fullStr == null) fullStr = '';
                    if (fullStr.length <= RG_TASK_NAME_PREVIEW_LEN) return;
                    var n = RG_TASK_NAME_PREVIEW_LEN;
                    var pv = fullStr.slice(0, n) + '...';
                    var on = tn.classList.toggle('rg-task-name-expanded');
                    if (on) {
                        tn.textContent = fullStr;
                        tn.classList.add('whitespace-normal', 'break-words');
                        tn.setAttribute('title', '点击收起');
                    } else {
                        tn.textContent = pv;
                        tn.classList.remove('whitespace-normal', 'break-words');
                        tn.setAttribute('title', fullStr);
                    }
                    return;
                }
                var detBtn = e.target.closest && e.target.closest('.rg-btn-detail');
                if (detBtn) {
                    e.preventDefault();
                    var rid0 = parseInt(detBtn.getAttribute('data-id'), 10);
                    if (isNaN(rid0)) return;
                    var api0 = getPywebviewApi();
                    if (!api0 || !api0.getRegulationRunDetail) return;
                    var res0 = await api0.getRegulationRunDetail(rid0);
                    if (!res0 || !res0.success || !res0.data) {
                        var em0 = (res0 && res0.message) || '加载失败';
                        if (window.MessageBox && MessageBox.alert) MessageBox.alert(em0);
                        return;
                    }
                    _rgDetailRowCache = res0.data;
                    rgRenderRunDetailModal(res0.data);
                    rgOpenModal('modalRunDetail');
                    return;
                }
                var btn = e.target.closest && e.target.closest('.rg-btn-json');
                if (!btn) return;
                var rid = parseInt(btn.getAttribute('data-id'), 10);
                var kind = btn.getAttribute('data-kind');
                var api = getPywebviewApi();
                if (!api || !api.getRegulationRunDetail) return;
                var res = await api.getRegulationRunDetail(rid);
                if (!res || !res.success || !res.data) {
                    var em = (res && res.message) || '加载失败';
                    if (window.MessageBox && MessageBox.alert) MessageBox.alert(em);
                    return;
                }
                var d = res.data;
                try {
                    if (kind === 'trig') {
                        rgRenderTriggerModal(JSON.parse(d.trigger_snapshot_json || '{}'), d.trigger_source);
                        rgSetModalTrigStrategyBadge(d.strategy_name);
                        rgOpenModal('modalTrig');
                    } else if (kind === 'query') {
                        rgRenderQueryModal(JSON.parse(d.query_snapshot_json || '{}'));
                        rgOpenModal('modalQuery');
                    }
                } catch (err) {
                    if (window.MessageBox && MessageBox.alert) MessageBox.alert('JSON 解析失败: ' + err);
                }
            });
        }

        var pag = document.getElementById('rgRunsPaginationLinks');
        if (pag) {
            pag.addEventListener('click', function (e) {
                var b = e.target.closest && e.target.closest('[data-rg-page]');
                if (!b) return;
                e.preventDefault();
                var p = parseInt(b.getAttribute('data-rg-page'), 10);
                if (isNaN(p)) return;
                var tp = rgTotalPages();
                if (p < 1 || p > tp) return;
                rgPage = p;
                rgLoadList();
            });
        }

        var btnSearch = document.getElementById('rgRunsBtnSearch');
        if (btnSearch) {
            btnSearch.addEventListener('click', function () {
                rgPage = 1;
                rgLoadList();
            });
        }
        var btnReset = document.getElementById('rgRunsBtnReset');
        if (btnReset) {
            btnReset.addEventListener('click', function () {
                var iq = document.getElementById('rgRunsFQ');
                if (iq) iq.value = '';
                rgMountFilters();
                rgPage = 1;
                rgLoadList();
            });
        }
    }

    function rgInit() {
        rgMountFilters();
        rgMountPageSize();
        rgBindTabs();
        rgBindModals();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', rgInit);
    } else {
        rgInit();
    }
})();
