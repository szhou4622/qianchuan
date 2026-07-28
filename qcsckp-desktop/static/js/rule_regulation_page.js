/**
 * 规则化停投页：运行配置 + 监测指标（条件 UI 与追投类似；指标键与素材大屏调控数据列一致）
 */
(function () {
    /** 与 static/dashboard.html ASSIST_METRIC_COLUMNS 数值列、schema_pmc_roi2_assist_task 一致 */
    const DEFAULT_ROI2_METRIC = 'stat_cost_for_roi2_assist';
    const METRICS = [
        { key: 'stat_cost_for_roi2_assist', label: '调控消耗' },
        { key: 'total_pay_order_count_for_roi2_assist', label: '调控成交订单数' },
        { key: 'total_pay_order_gmv_include_coupon_for_roi2_assist', label: '调控成交金额' },
        { key: 'total_prepay_and_pay_order_roi2_assist', label: '调控支付ROI' },
        { key: 'show_cnt_for_roi2_assist', label: '调控展示次数' },
        { key: 'click_cnt_for_roi2_assist', label: '调控点击次数' },
        { key: 'ctr_for_roi2_assist', label: '调控点击率' },
        { key: 'convert_rate_for_roi2_assist', label: '调控转化率' },
        { key: 'total_cost_per_pay_order_for_roi2_assist', label: '调控成交订单成本' },
        { key: 'pay_convert_cost_for_roi2_assist', label: '调控成交成本' },
        { key: 'pay_convert_cnt_for_roi2_assist', label: '调控成交人数' },
        { key: 'total_order_settle_amount_for_roi2_1h_assist', label: '调控净成交金额' },
        { key: 'total_refund_order_gmv_for_roi2_1h_rate_assist', label: '调控1小时内退款率' },
        { key: 'total_prepay_and_pay_settle_roi2_1h_assist', label: '调控净成交ROI' },
        { key: 'total_pay_order_gmv_for_roi2_assist', label: '调控用户实际支付金额' },
        { key: 'total_pay_order_coupon_amount_for_roi2_assist', label: '调控成交智能优惠券金额' },
    ];
    const RATE_METRICS = new Set([
        'ctr_for_roi2_assist',
        'convert_rate_for_roi2_assist',
        'total_refund_order_gmv_for_roi2_1h_rate_assist',
    ]);
    function isRateMetric(m) {
        return !!(m && RATE_METRICS.has(m));
    }
    function clampRatePercentUi(v) {
        if (!Number.isFinite(v)) return 0;
        return Math.max(0, Math.min(100, v));
    }
    function formatRatePercentFieldValue(v) {
        const x = clampRatePercentUi(typeof v === 'number' ? v : parseFloat(String(v)));
        if (!Number.isFinite(x)) return '0';
        const s = x.toFixed(10).replace(/\.?0+$/, '');
        return s === '' ? '0' : s;
    }
    /** 将已保存的数值原样显示在输入框（百分数字面量，如 0.25 即 0.25%） */
    function assistPercentLikeToUiNumber(n) {
        const x = typeof n === 'number' ? n : parseFloat(String(n));
        if (!Number.isFinite(x)) return 0;
        return x;
    }
    function formatCondValForInput(metric, raw) {
        if (isRateMetric(metric)) {
            const pct = assistPercentLikeToUiNumber(raw != null && raw !== '' ? parseFloat(raw) : 0);
            return formatRatePercentFieldValue(pct);
        }
        const v = raw != null && raw !== '' ? parseFloat(raw) : 0;
        return Number.isFinite(v) ? String(v) : '0';
    }
    function applyCondValInputRules(inp, metric) {
        if (isRateMetric(metric)) {
            inp.min = '0';
            inp.max = '100';
            inp.step = 'any';
            inp.value = formatRatePercentFieldValue(parseFloat(inp.value));
        } else {
            inp.removeAttribute('min');
            inp.removeAttribute('max');
            inp.step = 'any';
            const v = parseFloat(inp.value);
            inp.value = Number.isFinite(v) ? String(v) : '0';
        }
    }
    const OPS = [
        { key: 'gt', label: '大于' },
        { key: 'gte', label: '大于或等于' },
        { key: 'lt', label: '小于' },
        { key: 'lte', label: '小于或等于' },
        { key: 'eq', label: '等于' },
    ];
    const GROUP_COMBINE_ITEMS = [
        { value: 'or', label: '满足任一组合即触发' },
        { value: 'and', label: '需同时满足所有组合' },
    ];

    const RR_MAX_STRATEGIES = 10;
    const RR_STRATEGY_TITLE_MAX_LEN = 32;
    const RG_VALIDATION_FAIL_HINT = '请修正标红项后再保存或启用停投';

    let _rrDdGlobalsBound = false;
    function rrCloseAllDropdowns() {
        document.querySelectorAll('.rr-dd.rr-dd-open').forEach((wrap) => {
            const panel = wrap._rrPanel;
            wrap.classList.remove('rr-dd-open');
            if (panel && !panel.hidden) {
                panel.hidden = true;
                if (panel.parentNode === document.body) wrap.appendChild(panel);
            }
        });
    }
    function positionDdPanel(btn, panel) {
        const r = btn.getBoundingClientRect();
        const vw = window.innerWidth;
        const pad = 8;
        let left = r.left;
        const w = Math.max(r.width, 160);
        if (left + w > vw - pad) left = Math.max(pad, vw - w - pad);
        panel.style.left = left + 'px';
        panel.style.top = r.bottom + 4 + 'px';
        panel.style.minWidth = w + 'px';
        panel.style.maxWidth = Math.min(420, vw - 2 * pad) + 'px';
    }
    function escapeHtml(s) {
        const d = document.createElement('div');
        d.textContent = s;
        return d.innerHTML;
    }
    function ensureRrDdGlobalClose() {
        if (_rrDdGlobalsBound) return;
        _rrDdGlobalsBound = true;
        document.addEventListener(
            'click',
            (e) => {
                if (e.target.closest && e.target.closest('.rr-dd')) return;
                rrCloseAllDropdowns();
            },
            true
        );
        const sc = document.querySelector('.tab-scroll');
        if (sc) sc.addEventListener('scroll', rrCloseAllDropdowns, { passive: true });
        window.addEventListener('resize', rrCloseAllDropdowns);
    }
    function createCustomDropdown(items, value, options) {
        const opt = options || {};
        const wrap = document.createElement('div');
        wrap.className = 'rr-dd ' + (opt.sm ? 'rr-dd--sm ' : '') + (opt.widthClass || '');

        const hidden = document.createElement('input');
        hidden.type = 'hidden';
        if (opt.hiddenClass) hidden.className = opt.hiddenClass;
        hidden.value = value;

        const cur = items.find((x) => x.value === value) || items[0];
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'rr-dd-trigger';
        btn.innerHTML =
            '<span class="rr-dd-label">' +
            escapeHtml(cur.label) +
            '</span><i data-lucide="chevron-down" class="rr-dd-chevron w-3.5 h-3.5 shrink-0 opacity-70"></i>';

        const panel = document.createElement('div');
        panel.className = 'rr-dd-panel';
        panel.hidden = true;
        panel.setAttribute('role', 'listbox');

        items.forEach((item) => {
            const ob = document.createElement('button');
            ob.type = 'button';
            ob.className = 'rr-dd-opt' + (item.value === value ? ' rr-dd-opt--active' : '');
            ob.textContent = item.label;
            ob.addEventListener('click', (e) => {
                e.stopPropagation();
                hidden.value = item.value;
                const lab = btn.querySelector('.rr-dd-label');
                if (lab) lab.textContent = item.label;
                panel.querySelectorAll('.rr-dd-opt').forEach((el) => {
                    el.classList.toggle('rr-dd-opt--active', el === ob);
                });
                rrCloseAllDropdowns();
                hidden.dispatchEvent(new Event('input', { bubbles: true }));
                if (typeof opt.onChange === 'function') opt.onChange(item.value);
            });
            panel.appendChild(ob);
        });

        wrap._rrPanel = panel;

        btn.addEventListener('click', (e) => {
            e.stopPropagation();
            ensureRrDdGlobalClose();
            const wasOpen = wrap.classList.contains('rr-dd-open');
            rrCloseAllDropdowns();
            if (wasOpen) return;
            document.body.appendChild(panel);
            panel.hidden = false;
            panel.style.position = 'fixed';
            panel.style.zIndex = '10050';
            positionDdPanel(btn, panel);
            wrap.classList.add('rr-dd-open');
            if (window.lucide) lucide.createIcons();
        });

        wrap.appendChild(hidden);
        wrap.appendChild(btn);
        wrap.appendChild(panel);
        return wrap;
    }

    function mountGroupCombine(selectedValue) {
        const mount = document.getElementById('groupCombineMount');
        if (!mount) return;
        let v = selectedValue;
        if (v === undefined || v === null) {
            const ex = document.getElementById('groupCombine');
            v = ex ? ex.value : 'or';
        }
        if (v !== 'and') v = 'or';
        rrCloseAllDropdowns();
        mount.innerHTML = '';
        const dd = createCustomDropdown(GROUP_COMBINE_ITEMS, v, { widthClass: 'w-full' });
        const h = dd.querySelector('input[type="hidden"]');
        h.id = 'groupCombine';
        mount.appendChild(dd);
        if (window.lucide) lucide.createIcons();
    }

    function getApi() {
        try {
            if (window.top && window.top.pywebviewAPI) return window.top.pywebviewAPI;
        } catch (_) {}
        if (window.pywebview && window.pywebview.api) return window.pywebview.api;
        return null;
    }

    async function waitForApi(maxAttempts) {
        const n = maxAttempts == null ? 60 : maxAttempts;
        for (let i = 0; i < n; i++) {
            const api = getApi();
            if (api && api.getRuleRegulationConfig) return api;
            await new Promise((r) => setTimeout(r, 200));
        }
        return null;
    }

    function notifyErr(msg) {
        if (window.showMsgBox) window.showMsgBox(msg, 'error');
        else if (typeof window.appAlert === 'function') window.appAlert(msg, '错误');
    }
    function notifyOk(msg) {
        if (window.showMsgBox) window.showMsgBox(msg, 'success');
        else if (typeof window.appAlert === 'function') window.appAlert(msg, '提示');
    }

    function hasLeadingZeroBad(str) {
        const s = String(str).trim();
        if (!s) return false;
        return /^0\d/.test(s);
    }

    /** 与追投保存一致：格式、率类范围，且「大于/大于等于」时阈值须大于 0 */
    function triggerCondValueErrorMsg(raw, metric, op) {
        const o = String(op || 'gt').trim().toLowerCase();
        const s = String(raw).trim();
        if (s === '') return '请输入触发条件数值';
        if (hasLeadingZeroBad(s)) return '不能以0开头，请正确输入';
        const n = parseFloat(s);
        if (!Number.isFinite(n)) return '请输入有效数字';
        if (isRateMetric(metric)) {
            if (n < 0 || n > 100) return '率类指标范围为 0～100（百分比）';
        }
        if ((o === 'gt' || o === 'gte') && n <= 0) {
            return '「大于 / 大于等于」的阈值须大于 0';
        }
        return '';
    }

    function setCondValFieldError(inp, errEl, msg) {
        if (!errEl) return;
        if (!msg) {
            errEl.textContent = '';
            errEl.classList.add('hidden');
            if (inp) inp.classList.remove('rr-input--invalid');
            return;
        }
        errEl.textContent = msg;
        errEl.classList.remove('hidden');
        if (inp) inp.classList.add('rr-input--invalid');
    }

    function clearTriggerConditionError() {
        const el = document.getElementById('errTriggerConditions');
        if (!el) return;
        el.textContent = '';
        el.classList.add('hidden');
    }

    function setTriggerConditionError(msg) {
        const el = document.getElementById('errTriggerConditions');
        if (!el) return;
        if (!msg) {
            clearTriggerConditionError();
            return;
        }
        el.textContent = msg;
        el.classList.remove('hidden');
    }

    function clearTriggerCondFieldErrors() {
        document.querySelectorAll('.cond-val-err').forEach((el) => {
            el.textContent = '';
            el.classList.add('hidden');
        });
        document.querySelectorAll('input.cond-val.rr-input--invalid').forEach((inp) => {
            inp.classList.remove('rr-input--invalid');
        });
    }

    function clearAllFieldErrors() {
        clearTriggerConditionError();
        clearTriggerCondFieldErrors();
        const targetErr = document.getElementById('errRegulationTarget');
        if (targetErr) {
            targetErr.textContent = '';
            targetErr.classList.add('hidden');
        }
    }

    let strategiesState = [];
    let promotionTargetsState = [];
    let activeStrategyIndex = 0;
    let strategyRenameIndex = null;

    function defaultStrategyTitle(index) {
        return '策略 ' + (index + 1);
    }

    function genStrategyId() {
        if (typeof crypto !== 'undefined' && crypto.randomUUID) return crypto.randomUUID();
        return 's_' + Date.now().toString(36) + '_' + Math.random().toString(36).slice(2, 10);
    }

    function escapeTargetHtml(value) {
        return String(value == null ? '' : value).replace(/[&<>"']/g, (c) => ({
            '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
        })[c]);
    }

    function syncRegulationTargetHint() {
        const uid = document.getElementById('regStrategyTargetUid')?.value || '';
        const target = promotionTargetsState.find((x) => x.target_uid === uid);
        const hint = document.getElementById('regStrategyTargetScene');
        if (hint) {
            hint.textContent = target
                ? `${target.promotion_scene === 'product' ? '推商品' : '推直播'} · ${target.plan_system === 'global' ? '传统全域' : target.plan_system === 'chengfang' ? '千川乘方' : '体系待确认'} · 账户 ${target.aadvid} · 计划 ${target.ad_id}`
                : '请先在“监控计划”页面添加并启用计划。';
        }
        const err = document.getElementById('errRegulationTarget');
        if (err && uid) {
            err.textContent = '';
            err.classList.add('hidden');
        }
    }

    async function loadRegulationTargetOptions(api) {
        if (!api?.listPromotionTargets) return;
        const res = await api.listPromotionTargets(true);
        promotionTargetsState = Array.isArray(res?.data) ? res.data : [];
        const select = document.getElementById('regStrategyTargetUid');
        if (!select) return;
        const current = select.value;
        select.innerHTML = '<option value="">请选择监控计划</option>' + promotionTargetsState.map((x) =>
            `<option value="${escapeTargetHtml(x.target_uid)}">${x.promotion_scene === 'product' ? '推商品' : '推直播'}｜${x.plan_system === 'global' ? '传统全域' : x.plan_system === 'chengfang' ? '千川乘方' : '体系待确认'}｜${escapeTargetHtml(x.plan_name || x.ad_id)}｜${escapeTargetHtml(x.aadvid)}</option>`
        ).join('');
        if (current) select.value = current;
        syncRegulationTargetHint();
    }

    function defaultTriggerPayload() {
        return {
            group_combine: 'or',
            groups: [{ join: 'and', conditions: [{ metric: DEFAULT_ROI2_METRIC, op: 'gt', value: 0 }] }],
        };
    }

    function defaultCondition() {
        return { metric: DEFAULT_ROI2_METRIC, op: 'gt', value: 0 };
    }
    function defaultGroup() {
        return { join: 'and', conditions: [defaultCondition()] };
    }

    let _groupsRef = [];
    let whitelistAssistIds = [];

    function syncTriggerGroupsFromDom() {
        const groupEls = Array.from(document.querySelectorAll('[data-group-index]')).sort(
            (a, b) =>
                parseInt(a.getAttribute('data-group-index'), 10) -
                parseInt(b.getAttribute('data-group-index'), 10)
        );
        if (!groupEls.length) return;
        const next = [];
        groupEls.forEach((gel) => {
            const condEls = gel.querySelectorAll('[data-cond-index]');
            const conditions = [];
            condEls.forEach((cel) => {
                const metric = cel.querySelector('input.cond-metric')?.value;
                const op = cel.querySelector('input.cond-op')?.value;
                const raw = cel.querySelector('.cond-val')?.value;
                let val = parseFloat(raw);
                const m = metric || DEFAULT_ROI2_METRIC;
                if (isRateMetric(m)) val = clampRatePercentUi(val);
                else if (!Number.isFinite(val)) val = 0;
                conditions.push({ metric: m, op: op || 'gt', value: val });
            });
            if (conditions.length) next.push({ join: 'and', conditions });
        });
        if (!next.length) return;
        _groupsRef.length = 0;
        next.forEach((g) => _groupsRef.push(g));
    }

    function renderTriggerGroups(groups) {
        rrCloseAllDropdowns();
        const host = document.getElementById('triggerGroups');
        if (!host) return;
        host.innerHTML = '';
        groups.forEach((g, gi) => {
            const wrap = document.createElement('div');
            wrap.className =
                'rounded-xl pl-4 pr-3 py-3 space-y-2.5 bg-slate-950/40 border border-slate-600/25 border-l-[3px] border-l-sky-500/50 shadow-sm';
            wrap.setAttribute('data-group-index', String(gi));
            const header = document.createElement('div');
            header.className = 'flex flex-wrap items-center justify-between gap-2 mb-1';
            const title = document.createElement('span');
            title.className = 'text-xs font-semibold text-slate-200 tracking-wide';
            title.textContent = '条件组 ' + (gi + 1) + '（组内为「且」）';
            const right = document.createElement('div');
            right.className = 'flex items-center gap-2 shrink-0';
            const addBtn = document.createElement('button');
            addBtn.type = 'button';
            addBtn.className =
                'inline-flex items-center gap-0.5 text-xs font-medium px-2 py-1 rounded-md bg-sky-500/12 text-sky-300 hover:bg-sky-500/18 border border-sky-500/20';
            addBtn.textContent = '+ 条件';
            addBtn.addEventListener('click', () => {
                syncTriggerGroupsFromDom();
                const grp = _groupsRef[gi];
                if (!grp) return;
                if (!grp.conditions) grp.conditions = [];
                grp.conditions.push(defaultCondition());
                renderTriggerGroups(_groupsRef);
            });
            const rmGroup = document.createElement('button');
            rmGroup.type = 'button';
            rmGroup.className =
                'text-xs font-medium text-rose-400/85 hover:text-rose-300 px-1.5 py-0.5 rounded hover:bg-rose-500/10';
            rmGroup.textContent = '删除组';
            rmGroup.disabled = groups.length <= 1;
            rmGroup.classList.toggle('opacity-40', groups.length <= 1);
            rmGroup.addEventListener('click', () => {
                syncTriggerGroupsFromDom();
                if (_groupsRef.length <= 1) return;
                _groupsRef.splice(gi, 1);
                renderTriggerGroups(_groupsRef);
            });
            right.appendChild(addBtn);
            right.appendChild(rmGroup);
            header.appendChild(title);
            header.appendChild(right);
            wrap.appendChild(header);

            const condList = g.conditions && g.conditions.length ? g.conditions : [defaultCondition()];
            g.conditions = condList;
            condList.forEach((c, ci) => {
                const rowWrap = document.createElement('div');
                rowWrap.setAttribute('data-cond-index', String(ci));
                rowWrap.className = 'cond-row-block';

                const row = document.createElement('div');
                row.className = 'flex flex-wrap items-center gap-2';

                const errP = document.createElement('p');
                errP.className = 'cond-val-err rr-field-err hidden w-full';

                const inp = document.createElement('input');
                inp.type = 'number';
                inp.step = 'any';
                inp.className =
                    'cond-val num-input rr-input rr-input--flush w-28 max-w-full text-xs py-1.5 tabular-nums pr-2';
                const m0 = c.metric || DEFAULT_ROI2_METRIC;
                inp.value = formatCondValForInput(m0, c.value);
                applyCondValInputRules(inp, m0);

                const inpWrap = document.createElement('div');
                inpWrap.className = 'relative inline-flex items-center max-w-full shrink-0';
                const pctSuffix = document.createElement('span');
                pctSuffix.className =
                    'cond-pct-suffix pointer-events-none absolute right-2 top-1/2 -translate-y-1/2 text-slate-500 text-xs leading-none';
                pctSuffix.textContent = '%';
                pctSuffix.setAttribute('aria-hidden', 'true');
                inpWrap.appendChild(inp);
                inpWrap.appendChild(pctSuffix);

                const rangeHint = document.createElement('span');
                rangeHint.className = 'cond-rate-range-hint text-[10px] text-slate-500 whitespace-nowrap shrink-0';
                rangeHint.textContent = '取值范围: 0~100';

                const valCluster = document.createElement('div');
                valCluster.className = 'flex items-center gap-2 shrink-0 min-w-0';
                valCluster.appendChild(inpWrap);

                function setRateChrome(metric) {
                    const show = isRateMetric(metric);
                    pctSuffix.classList.toggle('hidden', !show);
                    rangeHint.classList.toggle('hidden', !show);
                    inp.classList.toggle('pr-7', show);
                    inp.classList.toggle('pr-2', !show);
                }
                setRateChrome(m0);

                const ms = createCustomDropdown(
                    METRICS.map((m) => ({ value: m.key, label: m.label })),
                    m0,
                    {
                        hiddenClass: 'cond-metric',
                        widthClass: 'rr-dd-metric shrink-0',
                        sm: true,
                        onChange: (newMetric) => {
                            applyCondValInputRules(inp, newMetric);
                            setRateChrome(newMetric);
                            setCondValFieldError(inp, errP, '');
                        },
                    }
                );
                const os = createCustomDropdown(
                    OPS.map((o) => ({ value: o.key, label: o.label })),
                    c.op || 'gt',
                    { hiddenClass: 'cond-op', widthClass: 'rr-dd-op shrink-0', sm: true }
                );
                os.querySelector('input.cond-op')?.addEventListener('input', () => {
                    setCondValFieldError(inp, errP, '');
                    rgScheduleDirtyCheck();
                });

                const syncRateOnBlur = () => {
                    const m = row.querySelector('input.cond-metric')?.value || DEFAULT_ROI2_METRIC;
                    if (!isRateMetric(m)) return;
                    inp.value = formatRatePercentFieldValue(parseFloat(inp.value));
                };
                const onCondValBlur = () => {
                    syncRateOnBlur();
                    const m = row.querySelector('input.cond-metric')?.value || DEFAULT_ROI2_METRIC;
                    const op0 = row.querySelector('input.cond-op')?.value || 'gt';
                    const msg = triggerCondValueErrorMsg(inp.value, m, op0);
                    setCondValFieldError(inp, errP, msg);
                };
                inp.addEventListener('blur', onCondValBlur);
                inp.addEventListener('change', syncRateOnBlur);
                inp.addEventListener('input', () => setCondValFieldError(inp, errP, ''));

                const rm = document.createElement('button');
                rm.type = 'button';
                rm.className = 'text-xs text-slate-500 hover:text-rose-400 px-1.5 py-0.5 rounded hover:bg-slate-800/80';
                rm.textContent = '删除';
                rm.addEventListener('click', () => {
                    syncTriggerGroupsFromDom();
                    const grp = _groupsRef[gi];
                    if (!grp || !grp.conditions || grp.conditions.length <= 1) return;
                    grp.conditions.splice(ci, 1);
                    renderTriggerGroups(_groupsRef);
                });

                row.appendChild(ms);
                row.appendChild(os);
                row.appendChild(valCluster);
                row.appendChild(rm);
                row.appendChild(rangeHint);
                rowWrap.appendChild(row);
                rowWrap.appendChild(errP);
                wrap.appendChild(rowWrap);
            });

            host.appendChild(wrap);
        });
        if (window.lucide) lucide.createIcons();
        rgScheduleDirtyCheck();
    }

    function validateTriggerConditions() {
        clearTriggerCondFieldErrors();
        syncTriggerGroupsFromDom();
        const groupEls = document.querySelectorAll('[data-group-index]');
        const condRows = document.querySelectorAll('.cond-row-block[data-cond-index]');
        if (!groupEls.length || !condRows.length) {
            setTriggerConditionError('请至少添加一条监测触发条件');
            const errEl = document.getElementById('errTriggerConditions');
            return {
                ok: false,
                firstMsg: '请至少添加一条监测触发条件',
                firstScrollEl: errEl || document.getElementById('triggerGroups'),
            };
        }
        setTriggerConditionError('');
        let ok = true;
        let firstMsg = '';
        let firstScrollEl = null;
        condRows.forEach((rowBlock) => {
            const inp = rowBlock.querySelector('input.cond-val');
            const metric = rowBlock.querySelector('input.cond-metric')?.value || DEFAULT_ROI2_METRIC;
            const op = rowBlock.querySelector('input.cond-op')?.value || 'gt';
            const errEl = rowBlock.querySelector('.cond-val-err');
            if (!inp) return;
            const msg = triggerCondValueErrorMsg(inp.value, metric, op);
            if (msg) {
                setCondValFieldError(inp, errEl, msg);
                ok = false;
                if (!firstMsg) firstMsg = msg;
                if (!firstScrollEl) firstScrollEl = inp;
            }
        });
        if (!ok) {
            setTriggerConditionError('请修正下方标红项');
            return { ok: false, firstMsg, firstScrollEl };
        }
        setTriggerConditionError('');
        return { ok: true, firstMsg: '', firstScrollEl: null };
    }

    function scrollRgValidationTargetIntoView(el) {
        if (!el || !(el instanceof Element)) return;
        try {
            el.scrollIntoView({ block: 'center', behavior: 'smooth', inline: 'nearest' });
        } catch (e) {}
    }

    function validateAllStrategiesBeforeSave() {
        clearAllFieldErrors();
        syncCurrentStrategyFromDom();
        const savedIdx = activeStrategyIndex;
        for (let i = 0; i < strategiesState.length; i++) {
            activeStrategyIndex = i;
            applyStrategyToDom(i);
            clearAllFieldErrors();
            const targetUid = document.getElementById('regStrategyTargetUid')?.value || '';
            if (!targetUid) {
                const targetErr = document.getElementById('errRegulationTarget');
                if (targetErr) {
                    targetErr.textContent = '请选择本策略所属的监控计划';
                    targetErr.classList.remove('hidden');
                }
                renderStrategyTabs();
                applyStrategyToDom(i);
                if (targetErr) {
                    targetErr.textContent = '请选择本策略所属的监控计划';
                    targetErr.classList.remove('hidden');
                }
                scrollRgValidationTargetIntoView(document.getElementById('regStrategyTargetUid'));
                activeStrategyIndex = i;
                return {
                    ok: false,
                    vt: { ok: false, firstMsg: '请选择监控计划' },
                    failedStrategyIndex: i,
                };
            }
            const vt = validateTriggerConditions();
            if (!vt.ok) {
                renderStrategyTabs();
                applyStrategyToDom(i);
                validateTriggerConditions();
                if (window.lucide) lucide.createIcons();
                scrollRgValidationTargetIntoView(vt.firstScrollEl);
                activeStrategyIndex = i;
                return { ok: false, vt, failedStrategyIndex: i };
            }
        }
        activeStrategyIndex = savedIdx;
        renderStrategyTabs();
        applyStrategyToDom(savedIdx);
        if (window.lucide) lucide.createIcons();
        return { ok: true };
    }

    function syncCurrentStrategyFromDom() {
        flushStrategyRename();
        if (!strategiesState.length) {
            strategiesState.push({
                id: genStrategyId(),
                title: defaultStrategyTitle(0),
                target_uid: '',
                trigger: defaultTriggerPayload(),
                regulation_stop_action: 'pause',
            });
            activeStrategyIndex = 0;
        }
        syncTriggerGroupsFromDom();
        const gcEl = document.getElementById('groupCombine');
        const group_combine = gcEl ? gcEl.value : 'or';
        const trig = { group_combine, groups: JSON.parse(JSON.stringify(_groupsRef)) };
        const stopAct = document.getElementById('btnStopActionDelete').classList.contains('seg-btn-active')
            ? 'delete'
            : 'pause';
        const cur = strategiesState[activeStrategyIndex];
        if (!cur) return;
        strategiesState[activeStrategyIndex] = {
            id: cur.id || genStrategyId(),
            title: cur.title || defaultStrategyTitle(activeStrategyIndex),
            target_uid: document.getElementById('regStrategyTargetUid')?.value || '',
            trigger: trig,
            regulation_stop_action: stopAct,
        };
    }

    function collectPayload() {
        const enabled = document.getElementById('cfgEnabled').checked;
        const headless = document.getElementById('btnHeadless').classList.contains('seg-btn-active');
        syncCurrentStrategyFromDom();
        const browser_executable_path = (
            document.getElementById('rgBrowserExecutable')?.value || ''
        ).trim();
        return {
            enabled,
            browser_headless: headless,
            browser_executable_path,
            trigger_query_period: '1h',
            whitelist_assist_ids: whitelistAssistIds.slice(),
            strategies: strategiesState.map((s) => ({
                id: s.id,
                title: s.title,
                target_uid: s.target_uid || '',
                trigger: s.trigger,
                regulation_stop_action: s.regulation_stop_action === 'delete' ? 'delete' : 'pause',
            })),
        };
    }

    function finishStrategyRename(idx, inputEl) {
        if (strategyRenameIndex !== idx || !strategiesState[idx]) return;
        let t = (inputEl && inputEl.value != null ? String(inputEl.value) : '').trim();
        if (!t) t = defaultStrategyTitle(idx);
        t = t.slice(0, RR_STRATEGY_TITLE_MAX_LEN);
        strategiesState[idx].title = t;
        strategyRenameIndex = null;
        renderStrategyTabs();
        try {
            if (window.lucide) lucide.createIcons();
        } catch (e) {}
        rgScheduleDirtyCheck();
    }

    function flushStrategyRename() {
        if (strategyRenameIndex === null) return;
        const inp = document.querySelector(
            '#strategyTabsBar input[data-strategy-rename="' + strategyRenameIndex + '"]'
        );
        if (inp) finishStrategyRename(strategyRenameIndex, inp);
    }

    function deleteStrategy(idx) {
        if (strategiesState.length <= 1) {
            notifyErr('至少保留一条策略');
            return;
        }
        if (idx < 0 || idx >= strategiesState.length) return;
        flushStrategyRename();
        syncCurrentStrategyFromDom();
        const oldActive = activeStrategyIndex;
        strategiesState.splice(idx, 1);
        if (idx < oldActive) activeStrategyIndex = oldActive - 1;
        else if (idx === oldActive) activeStrategyIndex = Math.min(oldActive, strategiesState.length - 1);
        strategyRenameIndex = null;
        renderStrategyTabs();
        applyStrategyToDom(activeStrategyIndex);
        try {
            if (window.lucide) lucide.createIcons();
        } catch (e) {}
        rgScheduleDirtyCheck();
    }

    function renderWhitelist() {
        const host = document.getElementById('whitelistTags');
        if (!host) return;
        host.innerHTML = '';
        whitelistAssistIds.forEach(function (id, idx) {
            const tag = document.createElement('div');
            tag.className = 'inline-flex items-center gap-1 px-2 py-1 rounded-md bg-emerald-500/15 text-emerald-200 border border-emerald-500/25 text-xs';
            const span = document.createElement('span');
            span.className = 'font-mono tabular-nums';
            span.textContent = String(id);
            const btn = document.createElement('button');
            btn.type = 'button';
            btn.className = 'inline-flex items-center justify-center p-0.5 rounded hover:bg-emerald-500/20 text-emerald-300 hover:text-emerald-100';
            btn.innerHTML = '<i data-lucide="x" class="w-3 h-3"></i>';
            btn.setAttribute('aria-label', '删除');
            btn.addEventListener('click', function () {
                removeWhitelistId(idx);
            });
            tag.appendChild(span);
            tag.appendChild(btn);
            host.appendChild(tag);
        });
        if (window.lucide) lucide.createIcons();
    }

    function adjustWhitelistHeight() {
        const el = document.getElementById('whitelistInput');
        if (!el) return;
        if (!el.value) {
            el.style.height = '';
            el.style.overflowY = 'hidden';
            return;
        }
        el.style.height = 'auto';
        var maxH = 88;
        var h = el.scrollHeight;
        el.style.height = Math.min(h, maxH) + 'px';
        el.style.overflowY = h > maxH ? 'auto' : 'hidden';
    }

    function addWhitelistId() {
        const inp = document.getElementById('whitelistInput');
        if (!inp) return;
        const raw = String(inp.value || '').trim();
        if (!raw) return;
        const ids = raw.split(/[,，\s]+/).map(function (s) { return s.trim(); }).filter(function (s) { return !!s; });
        let added = 0;
        ids.forEach(function (id) {
            if (whitelistAssistIds.indexOf(id) === -1) {
                whitelistAssistIds.push(id);
                added++;
            }
        });
        if (added) {
            renderWhitelist();
            rgScheduleDirtyCheck();
        }
        inp.value = '';
        adjustWhitelistHeight();
    }

    function removeWhitelistId(idx) {
        if (idx < 0 || idx >= whitelistAssistIds.length) return;
        whitelistAssistIds.splice(idx, 1);
        renderWhitelist();
        rgScheduleDirtyCheck();
    }

    function renderStrategyTabs() {
        const bar = document.getElementById('strategyTabsBar');
        if (!bar) return;
        const TAG_SHELL =
            'rr-strategy-tag shrink-0 border text-xs font-medium transition-colors min-h-[2.25rem] max-w-[14rem]';
        const TAG_ON = 'bg-cyan-500/20 text-cyan-200 border-cyan-500/35';
        const TAG_OFF = 'bg-slate-800/50 text-slate-400 border-slate-600/30 hover:border-slate-500/50';
        bar.innerHTML = '';
        strategiesState.forEach((st, idx) => {
            const isActive = idx === activeStrategyIndex;
            const isRenaming = strategyRenameIndex === idx;
            if (isRenaming) {
                const tag = document.createElement('div');
                tag.className = TAG_SHELL + ' ' + TAG_ON + ' flex items-stretch';
                const inp = document.createElement('input');
                inp.type = 'text';
                inp.setAttribute('data-strategy-rename', String(idx));
                inp.setAttribute('aria-label', '策略名称');
                inp.maxLength = RR_STRATEGY_TITLE_MAX_LEN;
                inp.autocomplete = 'off';
                inp.className =
                    'rr-strategy-tab-inp flex-1 min-w-0 min-h-[2.25rem] w-full px-2 py-1.5 border-0 rounded-none text-sm bg-transparent outline-none box-border';
                inp.value = (st.title && String(st.title).trim()) || defaultStrategyTitle(idx);
                inp.addEventListener('keydown', (e) => {
                    if (e.key === 'Enter') {
                        e.preventDefault();
                        inp.blur();
                    }
                });
                inp.addEventListener('click', (e) => e.stopPropagation());
                inp.addEventListener('blur', () => finishStrategyRename(idx, inp));
                tag.appendChild(inp);
                bar.appendChild(tag);
                return;
            }
            const tag = document.createElement('div');
            tag.className = TAG_SHELL + ' ' + (isActive ? TAG_ON : TAG_OFF);

            const main = document.createElement('button');
            main.type = 'button';
            main.className =
                'rr-strategy-tag-main py-1.5 truncate ' +
                (strategiesState.length > 1 ? 'pl-2 pr-0.5' : 'px-2');
            main.textContent = (st.title && String(st.title).trim()) || defaultStrategyTitle(idx);
            main.setAttribute('data-strategy-index', String(idx));
            main.addEventListener('click', () => {
                const j = parseInt(main.getAttribute('data-strategy-index'), 10);
                if (j === activeStrategyIndex) return;
                syncCurrentStrategyFromDom();
                activeStrategyIndex = j;
                renderStrategyTabs();
                applyStrategyToDom(activeStrategyIndex);
                try {
                    if (window.lucide) lucide.createIcons();
                } catch (e) {}
                rgScheduleDirtyCheck();
            });
            main.addEventListener('dblclick', (e) => {
                e.preventDefault();
                e.stopPropagation();
                syncCurrentStrategyFromDom();
                const j = parseInt(main.getAttribute('data-strategy-index'), 10);
                if (activeStrategyIndex !== j) {
                    activeStrategyIndex = j;
                    applyStrategyToDom(j);
                }
                strategyRenameIndex = j;
                renderStrategyTabs();
            });

            tag.appendChild(main);

            if (strategiesState.length > 1) {
                const btnX = document.createElement('button');
                btnX.type = 'button';
                btnX.className = 'rr-strategy-tag-x';
                btnX.setAttribute('aria-label', '删除该策略');
                btnX.title = '删除';
                const ix = document.createElement('i');
                ix.setAttribute('data-lucide', 'x');
                ix.className = 'w-3.5 h-3.5';
                btnX.appendChild(ix);
                btnX.addEventListener('click', (e) => {
                    e.preventDefault();
                    e.stopPropagation();
                    deleteStrategy(idx);
                });
                tag.appendChild(btnX);
            }

            bar.appendChild(tag);
        });
        if (strategiesState.length < RR_MAX_STRATEGIES) {
            const add = document.createElement('button');
            add.type = 'button';
            add.className =
                'inline-flex items-center gap-1 px-2.5 py-1.5 rounded-lg text-xs font-medium border border-dashed border-slate-600/50 text-slate-500 hover:text-cyan-300 hover:border-cyan-500/35 min-h-[2.25rem] transition-colors';
            const plus = document.createElement('i');
            plus.setAttribute('data-lucide', 'plus');
            plus.className = 'w-3.5 h-3.5 shrink-0';
            add.appendChild(plus);
            add.appendChild(document.createTextNode(' 新建策略'));
            add.addEventListener('click', () => addStrategy());
            bar.appendChild(add);
        }
        try {
            if (window.lucide) lucide.createIcons();
        } catch (e) {}
        if (strategyRenameIndex !== null) {
            requestAnimationFrame(() => {
                const el = document.querySelector(
                    '#strategyTabsBar input[data-strategy-rename="' + strategyRenameIndex + '"]'
                );
                if (el) {
                    el.focus();
                    const len = el.value.length;
                    el.setSelectionRange(len, len);
                }
            });
        }
    }

    function addStrategy() {
        if (strategiesState.length >= RR_MAX_STRATEGIES) {
            notifyErr('最多 ' + RR_MAX_STRATEGIES + ' 条策略');
            return;
        }
        syncCurrentStrategyFromDom();
        const newIdx = strategiesState.length;
        strategiesState.push({
            id: genStrategyId(),
            title: defaultStrategyTitle(newIdx),
            target_uid: '',
            trigger: defaultTriggerPayload(),
            regulation_stop_action: 'pause',
        });
        activeStrategyIndex = strategiesState.length - 1;
        strategyRenameIndex = activeStrategyIndex;
        renderStrategyTabs();
        applyStrategyToDom(activeStrategyIndex);
        rgScheduleDirtyCheck();
    }

    function applyStopActionToDom(action) {
        const del = (action || 'pause') === 'delete';
        const bp = document.getElementById('btnStopActionPause');
        const bd = document.getElementById('btnStopActionDelete');
        if (bp && bd) {
            bp.classList.toggle('seg-btn-active', !del);
            bd.classList.toggle('seg-btn-active', del);
        }
        if (window.lucide) lucide.createIcons();
    }

    function applyStrategyToDom(index) {
        const s = strategiesState[index];
        if (!s) return;
        const targetSelect = document.getElementById('regStrategyTargetUid');
        if (targetSelect) targetSelect.value = s.target_uid || '';
        syncRegulationTargetHint();
        const t = s.trigger || defaultTriggerPayload();
        mountGroupCombine(t.group_combine === 'and' ? 'and' : 'or');
        _groupsRef = JSON.parse(JSON.stringify(t.groups && t.groups.length ? t.groups : [defaultGroup()]));
        renderTriggerGroups(_groupsRef);
        applyStopActionToDom(s.regulation_stop_action);
    }

    function applyData(data) {
        if (!data || data.success === false) return;
        document.getElementById('cfgEnabled').checked = !!data.enabled;
        syncEnabledLabel();
        const hl = !!data.browser_headless;
        document.getElementById('btnHeadful').classList.toggle('seg-btn-active', !hl);
        document.getElementById('btnHeadless').classList.toggle('seg-btn-active', hl);
        const rgBx = document.getElementById('rgBrowserExecutable');
        if (rgBx) rgBx.value = (data.browser_executable_path != null) ? String(data.browser_executable_path) : '';

        strategyRenameIndex = null;
        const legacyRootStop = data.regulation_stop_action;
        function normStrategyStopAct(raw) {
            if (raw === 'delete' || raw === 'pause') return raw;
            if (legacyRootStop === 'delete' || legacyRootStop === 'pause') return legacyRootStop;
            return 'pause';
        }
        if (Array.isArray(data.strategies) && data.strategies.length) {
            strategiesState = data.strategies.map((s, i) => ({
                id: s.id || genStrategyId(),
                title: (s.title || '').trim() || defaultStrategyTitle(i),
                target_uid: s.target_uid || '',
                trigger: s.trigger || defaultTriggerPayload(),
                regulation_stop_action: normStrategyStopAct(s.regulation_stop_action),
            }));
        } else {
            const t = data.trigger || defaultTriggerPayload();
            strategiesState = [
                {
                    id: genStrategyId(),
                    title: defaultStrategyTitle(0),
                    target_uid: '',
                    trigger: t,
                    regulation_stop_action: normStrategyStopAct(undefined),
                },
            ];
        }
        activeStrategyIndex = 0;
        renderStrategyTabs();
        applyStrategyToDom(0);
        // 追投白名单
        if (Array.isArray(data.whitelist_assist_ids)) {
            whitelistAssistIds = data.whitelist_assist_ids.map(function (x) { return String(x).trim(); }).filter(function (x) { return !!x; });
        } else {
            whitelistAssistIds = [];
        }
        renderWhitelist();
        rgMarkSavedSnapshot();
    }

    function syncEnabledLabel() {
        const on = document.getElementById('cfgEnabled').checked;
        const el = document.getElementById('enabledLabel');
        el.textContent = on ? '已启用' : '关闭';
        el.className = on
            ? 'text-xs text-emerald-400 shrink-0 tabular-nums min-w-[2.5rem]'
            : 'text-xs text-slate-500 shrink-0 tabular-nums min-w-[2.5rem]';
    }

    let rgLastSavedSnapshot = null;
    let rgDirtyTimer = null;

    function rgSnapshotPayloadString() {
        try {
            syncCurrentStrategyFromDom();
            return JSON.stringify(collectPayload());
        } catch (e) {
            return '';
        }
    }

    function rgRefreshUnsavedHint() {
        const el = document.getElementById('rgConfigUnsavedHint');
        if (!el) return;
        if (rgLastSavedSnapshot === null) {
            el.classList.add('hidden');
            return;
        }
        const cur = rgSnapshotPayloadString();
        el.classList.toggle('hidden', cur === rgLastSavedSnapshot);
    }

    function rgMarkSavedSnapshot() {
        rgLastSavedSnapshot = rgSnapshotPayloadString();
        rgRefreshUnsavedHint();
    }

    function rgScheduleDirtyCheck() {
        if (rgDirtyTimer) clearTimeout(rgDirtyTimer);
        rgDirtyTimer = window.setTimeout(function () {
            rgDirtyTimer = null;
            rgRefreshUnsavedHint();
        }, 80);
    }

    let _triggerGroupBtnBound = false;
    function bindTriggerGroupRef() {
        if (_triggerGroupBtnBound) return;
        _triggerGroupBtnBound = true;
        const btn = document.getElementById('btnAddGroup');
        if (!btn) return;
        btn.addEventListener('click', () => {
            syncTriggerGroupsFromDom();
            _groupsRef.push(defaultGroup());
            renderTriggerGroups(_groupsRef);
            rgScheduleDirtyCheck();
        });
    }

    async function init() {
        bindTriggerGroupRef();
        document.getElementById('btnHeadful').addEventListener('click', () => {
            document.getElementById('btnHeadful').classList.add('seg-btn-active');
            document.getElementById('btnHeadless').classList.remove('seg-btn-active');
            rgScheduleDirtyCheck();
        });
        document.getElementById('btnHeadless').addEventListener('click', () => {
            document.getElementById('btnHeadless').classList.add('seg-btn-active');
            document.getElementById('btnHeadful').classList.remove('seg-btn-active');
            rgScheduleDirtyCheck();
        });
        const rgBx = document.getElementById('rgBrowserExecutable');
        if (rgBx) {
            rgBx.addEventListener('input', () => rgScheduleDirtyCheck());
            rgBx.addEventListener('change', () => rgScheduleDirtyCheck());
        }

        document.getElementById('groupCombineMount').addEventListener('input', () => rgScheduleDirtyCheck());
        document.getElementById('regStrategyTargetUid')?.addEventListener('change', () => {
            syncRegulationTargetHint();
            rgScheduleDirtyCheck();
        });

        document.getElementById('btnReload').addEventListener('click', async () => {
            const api = await waitForApi();
            if (!api || !api.getRuleRegulationConfig) {
                notifyErr('无法连接本地服务');
                return;
            }
            try {
                await loadRegulationTargetOptions(api);
                const data = await api.getRuleRegulationConfig();
                applyData(data);
                notifyOk('已重新加载');
            } catch (e) {
                notifyErr(String(e && e.message ? e.message : e));
            }
        });

        document.getElementById('btnSave').addEventListener('click', async () => {
            const vr = validateAllStrategiesBeforeSave();
            if (!vr.ok) {
                notifyErr(RG_VALIDATION_FAIL_HINT);
                return;
            }
            const api = await waitForApi();
            if (!api || !api.setRuleRegulationConfig) {
                notifyErr('无法连接本地服务');
                return;
            }
            try {
                const res = await api.setRuleRegulationConfig(collectPayload());
                if (res && res.success) {
                    applyData(res);
                    notifyOk('已保存');
                } else {
                    notifyErr((res && res.message) || '保存失败');
                }
            } catch (e) {
                notifyErr(String(e && e.message ? e.message : e));
            }
        });

        function setManualRegModalMessage(kind, text) {
            const al = document.getElementById('manualRegModalAlert');
            if (!al) return;
            if (!text) {
                al.classList.add('hidden');
                al.textContent = '';
                return;
            }
            al.classList.remove('hidden');
            al.textContent = text;
            al.className =
                'rounded-lg px-3 py-2.5 text-xs leading-snug border ' +
                (kind === 'success'
                    ? 'border-emerald-500/45 bg-emerald-950/50 text-emerald-100'
                    : 'border-rose-500/45 bg-rose-950/40 text-rose-100');
        }
        function clearManualRegModalMessage() {
            setManualRegModalMessage('info', '');
        }
        function openManualRegModal() {
            const m = document.getElementById('modalManualRegStop');
            if (!m) return;
            clearManualRegModalMessage();
            m.classList.remove('hidden');
            m.setAttribute('aria-hidden', 'false');
            if (window.lucide) lucide.createIcons();
        }
        function closeManualRegModal() {
            const m = document.getElementById('modalManualRegStop');
            if (!m) return;
            m.classList.add('hidden');
            m.setAttribute('aria-hidden', 'true');
        }
        function getMrgStopAction() {
            const del = document.getElementById('btnMrgDelete');
            return del && del.classList.contains('seg-btn-active') ? 'delete' : 'pause';
        }

        const btnMrgPause = document.getElementById('btnMrgPause');
        const btnMrgDelete = document.getElementById('btnMrgDelete');
        if (btnMrgPause && btnMrgDelete) {
            btnMrgPause.addEventListener('click', () => {
                btnMrgPause.classList.add('seg-btn-active');
                btnMrgDelete.classList.remove('seg-btn-active');
            });
            btnMrgDelete.addEventListener('click', () => {
                btnMrgDelete.classList.add('seg-btn-active');
                btnMrgPause.classList.remove('seg-btn-active');
            });
        }

        document.getElementById('btnManualRegStop')?.addEventListener('click', () => {
            openManualRegModal();
        });
        document.getElementById('btnManualRegClose')?.addEventListener('click', () => closeManualRegModal());
        document.getElementById('btnManualRegCancel')?.addEventListener('click', () => closeManualRegModal());
        document.getElementById('modalManualRegStopBackdrop')?.addEventListener('click', () => closeManualRegModal());

        document.getElementById('btnManualRegSubmit')?.addEventListener('click', async () => {
            const aidEl = document.getElementById('mrgAssistId');
            const ai = String((aidEl && aidEl.value) || '').trim();
            if (!ai) {
                setManualRegModalMessage('error', '请填写调控任务 ID');
                return;
            }
            const api = await waitForApi();
            if (!api || !api.runImmediateRegulationStopPrepare) {
                setManualRegModalMessage('error', '无法连接本地服务');
                return;
            }
            const btn = document.getElementById('btnManualRegSubmit');
            const prev = btn ? btn.textContent : '';
            let successCloseScheduled = false;
            if (btn) {
                btn.disabled = true;
                btn.textContent = '处理中…';
            }
            clearManualRegModalMessage();
            try {
                const res = await api.runImmediateRegulationStopPrepare(ai, getMrgStopAction());
                if (res && res.success) {
                    setManualRegModalMessage('success', res.message || '操作已完成');
                    successCloseScheduled = true;
                    window.setTimeout(() => {
                        closeManualRegModal();
                        if (btn) {
                            btn.disabled = false;
                            btn.textContent = prev;
                        }
                    }, 1400);
                    return;
                }
                setManualRegModalMessage('error', (res && res.message) || '停投失败');
            } catch (e) {
                setManualRegModalMessage('error', String(e && e.message ? e.message : e));
            } finally {
                if (!successCloseScheduled && btn) {
                    btn.disabled = false;
                    btn.textContent = prev;
                }
            }
        });

        document.getElementById('cfgEnabled').addEventListener('change', async () => {
            const cb = document.getElementById('cfgEnabled');
            const prev = !cb.checked;
            const api = getApi();
            if (!api || !api.setRuleRegulationConfig) {
                cb.checked = prev;
                syncEnabledLabel();
                notifyErr('无法连接本地服务');
                return;
            }
            if (cb.checked) {
                const vr = validateAllStrategiesBeforeSave();
                if (!vr.ok) {
                    cb.checked = false;
                    syncEnabledLabel();
                    notifyErr(RG_VALIDATION_FAIL_HINT);
                    return;
                }
            }
            try {
                const res = await api.setRuleRegulationConfig(collectPayload());
                if (res && res.success) {
                    syncEnabledLabel();
                    rgMarkSavedSnapshot();
                } else {
                    cb.checked = prev;
                    syncEnabledLabel();
                    notifyErr((res && res.message) || '保存失败');
                }
            } catch (e) {
                cb.checked = prev;
                syncEnabledLabel();
                notifyErr(String(e && e.message ? e.message : e));
            }
        });

        const bp = document.getElementById('btnStopActionPause');
        const bd = document.getElementById('btnStopActionDelete');
        if (bp && bd) {
            bp.addEventListener('click', () => {
                bp.classList.add('seg-btn-active');
                bd.classList.remove('seg-btn-active');
                rgScheduleDirtyCheck();
                if (window.lucide) lucide.createIcons();
            });
            bd.addEventListener('click', () => {
                bd.classList.add('seg-btn-active');
                bp.classList.remove('seg-btn-active');
                rgScheduleDirtyCheck();
                if (window.lucide) lucide.createIcons();
            });
        }

        // 追投白名单事件绑定
        const wlInput = document.getElementById('whitelistInput');
        if (wlInput) {
            wlInput.addEventListener('input', adjustWhitelistHeight);
            wlInput.addEventListener('keydown', function (e) {
                if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
                    e.preventDefault();
                    addWhitelistId();
                }
            });
        }
        document.getElementById('btnAddWhitelist')?.addEventListener('click', addWhitelistId);
        document.getElementById('btnClearWhitelistInput')?.addEventListener('click', function () {
            const inp = document.getElementById('whitelistInput');
            if (inp) {
                inp.value = '';
                adjustWhitelistHeight();
            }
            if (whitelistAssistIds.length) {
                whitelistAssistIds = [];
                renderWhitelist();
                rgScheduleDirtyCheck();
            }
        });

        const api = await waitForApi();
        if (api && api.getRuleRegulationConfig) {
            try {
                await loadRegulationTargetOptions(api);
                const data = await api.getRuleRegulationConfig();
                applyData(data);
            } catch (e) {
                notifyErr(String(e && e.message ? e.message : e));
            }
        }
        ['input', 'change'].forEach((ev) => {
            document.getElementById('cfgEnabled').addEventListener(ev, rgScheduleDirtyCheck);
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
