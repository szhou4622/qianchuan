/**
 * 即刻追投弹窗字段校验（独立模块，与 rule_retargeting.html 主表单规则对齐）
 * 依赖 DOM：#errImm* / #imm* 元素；样式类 rr-field-err、rr-input--invalid
 */
(function (global) {
    'use strict';

    var RR_MIN_BUDGET_YUAN = 100;
    var RR_MAX_BID_YUAN = 10000;
    var RR_MIN_LIVE_BID_YUAN = 0.1;

    function immSetFieldError(inputEl, errEl, msg) {
        if (!errEl) return;
        if (!msg) {
            errEl.textContent = '';
            errEl.classList.add('hidden');
            if (inputEl) inputEl.classList.remove('rr-input--invalid');
            return;
        }
        errEl.textContent = msg;
        errEl.classList.remove('hidden');
        if (inputEl) inputEl.classList.add('rr-input--invalid');
    }

    function hasLeadingZeroBad(str) {
        var s = String(str).trim();
        if (!s) return false;
        return /^0\d/.test(s);
    }

    function hasMoreThanTwoDecimalPlaces(str) {
        var s = String(str).trim();
        var dot = s.indexOf('.');
        if (dot === -1) return false;
        var frac = s.slice(dot + 1).replace(/[^\d]/g, '');
        if (frac.length === 0) return true;
        return frac.length > 2;
    }

    /** 多个小数点或非法字符（如 100..2222） */
    function hasBadDecimalShape(raw) {
        var s = String(raw).trim();
        if (!s) return false;
        if ((s.match(/\./g) || []).length > 1) return true;
        if (/[^0-9.]/.test(s.replace(/^-/, ''))) return true;
        return false;
    }

    function budgetYuanErrorMsg(raw) {
        var s = String(raw).trim();
        if (s === '') return '';
        if (hasBadDecimalShape(raw)) return '请输入有效数字';
        if (hasLeadingZeroBad(s)) return '不能以0开头，请正确输入';
        if (hasMoreThanTwoDecimalPlaces(s)) return '仅支持最多2位小数';
        var n = parseFloat(s);
        if (!Number.isFinite(n)) return '请输入有效数字';
        if (n <= 0) return '预算需大于 0 元';
        if (n < RR_MIN_BUDGET_YUAN) return '预算需大于100元';
        return '';
    }

    function netRoiTargetErrorMsg(raw) {
        var s = String(raw).trim();
        if (s === '') return '';
        if (hasBadDecimalShape(raw)) return '支持范围: 0.01-100，最多两位小数';
        if (hasLeadingZeroBad(s)) return '不能以0开头，请正确输入';
        if (hasMoreThanTwoDecimalPlaces(s)) return '支持范围: 0.01-100，最多两位小数';
        var n = parseFloat(s);
        if (!Number.isFinite(n)) return '支持范围: 0.01-100，最多两位小数';
        if (n < 0.01 || n > 100) return '支持范围: 0.01-100，最多两位小数';
        return '';
    }

    function validateDurationText(v) {
        var x = parseFloat(v);
        if (!Number.isFinite(x)) return '调控时长范围 0.5～24 小时，请正确填写';
        if (x < 0.5 || x > 24) return '调控时长范围 0.5～24 小时，请正确填写';
        var steps = Math.round((x - 0.5) / 0.5);
        var expected = 0.5 + steps * 0.5;
        if (Math.abs(x - expected) > 1e-4) return '调控时长需为0.5的整数倍，请正确填写';
        return '';
    }

    function immMaterialIdErrorMsg(raw) {
        var s = String(raw).trim();
        if (s === '') return '请填写素材 ID';
        return '';
    }

    function immTaskSuffixErrorMsg(raw) {
        var s = String(raw);
        if (s.length > 15) return '后缀最多 15 个字';
        return '';
    }

    function getImmMethodGoal() {
        var mBtn = document.querySelector('.imm-method-btn.seg-btn-active');
        var gBtn = document.querySelector('.imm-goal-btn.seg-btn-active');
        return {
            method: (mBtn && mBtn.getAttribute('data-method')) || 'volume',
            goal: (gBtn && gBtn.getAttribute('data-goal')) || 'net_roi',
        };
    }

    /** @returns {boolean} 无错误为 true */
    function applyImmMaterialId() {
        var midEl = document.getElementById('immMaterialId');
        var m0 = immMaterialIdErrorMsg(midEl ? midEl.value : '');
        immSetFieldError(midEl, document.getElementById('errImmMaterialId'), m0);
        return !m0;
    }

    function applyImmVolBudget() {
        var mg = getImmMethodGoal();
        var vb = document.getElementById('immVolBudget');
        var errEl = document.getElementById('errImmVolBudget');
        if (mg.method !== 'volume') {
            immSetFieldError(vb, errEl, '');
            return true;
        }
        var vs = vb ? String(vb.value).trim() : '';
        var vbErr = vs === '' ? '请填写调控总预算' : budgetYuanErrorMsg(vb.value);
        immSetFieldError(vb, errEl, vbErr);
        return !vbErr;
    }

    function applyImmVolDuration() {
        var mg = getImmMethodGoal();
        var vd = document.getElementById('immVolDuration');
        var errEl = document.getElementById('errImmVolDuration');
        if (mg.method !== 'volume') {
            immSetFieldError(vd, errEl, '');
            return true;
        }
        var md = validateDurationText(vd ? vd.value : '');
        immSetFieldError(vd, errEl, md);
        return !md;
    }

    function applyImmCcDailyNet() {
        var mg = getImmMethodGoal();
        var netEl = document.getElementById('immCcDailyNet');
        var errEl = document.getElementById('errImmCcDailyNet');
        if (mg.method !== 'cost_control' || mg.goal !== 'net_roi') {
            immSetFieldError(netEl, errEl, '');
            return true;
        }
        var ns = netEl ? String(netEl.value).trim() : '';
        var n0 = ns === '' ? '请填写调控日预算' : budgetYuanErrorMsg(netEl.value);
        immSetFieldError(netEl, errEl, n0);
        return !n0;
    }

    function applyImmCcRoiTarget() {
        var mg = getImmMethodGoal();
        var roiEl = document.getElementById('immCcRoiTarget');
        var errEl = document.getElementById('errImmCcRoiTarget');
        if (mg.method !== 'cost_control' || mg.goal !== 'net_roi') {
            immSetFieldError(roiEl, errEl, '');
            return true;
        }
        var rs = roiEl ? String(roiEl.value).trim() : '';
        var roiErr = rs === '' ? '请输入期望的ROI目标' : netRoiTargetErrorMsg(roiEl.value);
        immSetFieldError(roiEl, errEl, roiErr);
        return !roiErr;
    }

    function applyImmCcDailyLive() {
        var mg = getImmMethodGoal();
        var liveEl = document.getElementById('immCcDailyLive');
        var errEl = document.getElementById('errImmCcDailyLive');
        if (mg.method !== 'cost_control' || mg.goal !== 'live_room') {
            immSetFieldError(liveEl, errEl, '');
            return true;
        }
        var ls = liveEl ? String(liveEl.value).trim() : '';
        var l0 = ls === '' ? '请填写调控日预算' : budgetYuanErrorMsg(liveEl.value);
        immSetFieldError(liveEl, errEl, l0);
        return !l0;
    }

    function applyImmCcBid() {
        var mg = getImmMethodGoal();
        var bidEl = document.getElementById('immCcBid');
        var bidErr = document.getElementById('errImmCcBid');
        if (mg.method !== 'cost_control' || mg.goal !== 'live_room') {
            immSetFieldError(bidEl, bidErr, '');
            return true;
        }
        var bs = bidEl ? String(bidEl.value).trim() : '';
        if (bs === '') {
            immSetFieldError(bidEl, bidErr, '出价不能为空');
            return false;
        }
        if (hasLeadingZeroBad(bs)) {
            immSetFieldError(bidEl, bidErr, '不能以0开头，请正确输入');
            return false;
        }
        if (hasBadDecimalShape(bs) || hasMoreThanTwoDecimalPlaces(bs)) {
            immSetFieldError(bidEl, bidErr, '仅支持最多2位小数');
            return false;
        }
        var bid = parseFloat(bs);
        if (!Number.isFinite(bid)) {
            immSetFieldError(bidEl, bidErr, '请输入有效数字');
            return false;
        }
        if (bid < RR_MIN_LIVE_BID_YUAN) {
            immSetFieldError(bidEl, bidErr, '出价不能低于0.1元');
            return false;
        }
        if (bid > RR_MAX_BID_YUAN) {
            immSetFieldError(bidEl, bidErr, '出价不能高于10,000元');
            return false;
        }
        var dbStr = String(document.getElementById('immCcDailyLive').value).trim();
        if (dbStr !== '') {
            var db = parseFloat(dbStr);
            if (Number.isFinite(db) && bid > db) {
                immSetFieldError(bidEl, bidErr, '出价不能高于预算');
                return false;
            }
        }
        immSetFieldError(bidEl, bidErr, '');
        return true;
    }

    function applyImmTaskSuffix() {
        var sfx = document.getElementById('immTaskSuffix');
        if (!sfx) return true;
        var sx = immTaskSuffixErrorMsg(sfx.value);
        immSetFieldError(sfx, document.getElementById('errImmTaskSuffix'), sx);
        return !sx;
    }

    function clearImmediateFieldErrors() {
        [
            ['immMaterialId', 'errImmMaterialId'],
            ['immVolBudget', 'errImmVolBudget'],
            ['immVolDuration', 'errImmVolDuration'],
            ['immCcDailyNet', 'errImmCcDailyNet'],
            ['immCcRoiTarget', 'errImmCcRoiTarget'],
            ['immCcDailyLive', 'errImmCcDailyLive'],
            ['immCcBid', 'errImmCcBid'],
            ['immTaskSuffix', 'errImmTaskSuffix'],
        ].forEach(function (pair) {
            var inp = document.getElementById(pair[0]);
            var err = document.getElementById(pair[1]);
            immSetFieldError(inp, err, '');
        });
    }

    /** 仅清除某一字段（输入时调用） */
    function clearImmediateErrorByInputId(inputId) {
        var map = {
            immMaterialId: 'errImmMaterialId',
            immVolBudget: 'errImmVolBudget',
            immVolDuration: 'errImmVolDuration',
            immCcDailyNet: 'errImmCcDailyNet',
            immCcRoiTarget: 'errImmCcRoiTarget',
            immCcDailyLive: 'errImmCcDailyLive',
            immCcBid: 'errImmCcBid',
            immTaskSuffix: 'errImmTaskSuffix',
        };
        var eid = map[inputId];
        if (!eid) return;
        immSetFieldError(document.getElementById(inputId), document.getElementById(eid), '');
    }

    /**
     * 失焦时仅校验当前（及联动）字段，不整表清空。
     * @param {string} inputId
     */
    function validateImmediateFieldOnBlur(inputId) {
        switch (inputId) {
            case 'immMaterialId':
                return applyImmMaterialId();
            case 'immVolBudget':
                return applyImmVolBudget();
            case 'immVolDuration':
                return applyImmVolDuration();
            case 'immCcDailyNet':
                return applyImmCcDailyNet();
            case 'immCcRoiTarget':
                return applyImmCcRoiTarget();
            case 'immCcDailyLive':
                applyImmCcDailyLive();
                return applyImmCcBid();
            case 'immCcBid':
                return applyImmCcBid();
            case 'immTaskSuffix':
                return applyImmTaskSuffix();
            default:
                return true;
        }
    }

    /**
     * @returns {{ ok: boolean, firstMsg: string }}
     */
    function validateImmediateModalFields() {
        clearImmediateFieldErrors();
        applyImmMaterialId();
        var mg = getImmMethodGoal();
        if (mg.method === 'volume') {
            applyImmVolBudget();
            applyImmVolDuration();
        } else if (mg.goal === 'net_roi') {
            applyImmCcDailyNet();
            applyImmCcRoiTarget();
        } else {
            applyImmCcDailyLive();
            applyImmCcBid();
        }
        applyImmTaskSuffix();

        var firstErr = document.querySelector('#modalImmediateRetarget .rr-field-err:not(.hidden)');
        var firstMsg = firstErr ? String(firstErr.textContent || '').trim() : '';
        var ok = !firstErr;
        return { ok: ok, firstMsg: firstMsg };
    }

    global.clearImmediateFieldErrors = clearImmediateFieldErrors;
    global.clearImmediateErrorByInputId = clearImmediateErrorByInputId;
    global.validateImmediateModalFields = validateImmediateModalFields;
    global.validateImmediateFieldOnBlur = validateImmediateFieldOnBlur;
})(typeof window !== 'undefined' ? window : this);
