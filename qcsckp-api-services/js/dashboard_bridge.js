/**
 * Web 端看板：将原 pywebview js_api 转为 POST /api/dashboard.php
 */
(function () {
    'use strict';

    const API = '/api/dashboard.php';

    async function post(body) {
        const r = await fetch(API, {
            method: 'POST',
            credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json; charset=utf-8' },
            body: JSON.stringify(body),
        });
        const text = await r.text();
        try {
            return JSON.parse(text);
        } catch {
            return { success: false, message: text || '响应解析失败' };
        }
    }

    const webApi = {
        async getTableData(period, sortBy, sortOrder, page, pageSize) {
            return post({
                action: 'table_data',
                period: period || '1h',
                sortBy: sortBy || 'costDiff',
                sortOrder: sortOrder || 'desc',
                page: page || 1,
                pageSize: pageSize || 50,
            });
        },

        async getMaterialHistoryRecent(materialId, limit) {
            // 素材 ID 常超过 JS 安全整数，必须用字符串传参，禁止依赖 JSON 数字
            const id =
                materialId != null && materialId !== ''
                    ? typeof materialId === 'string'
                        ? materialId
                        : String(materialId)
                    : '';
            return post({
                action: 'material_history',
                materialId: id,
                limit: limit || 200,
            });
        },

        async getTop20ByCost(hours) {
            return post({ action: 'top20', hours: hours != null ? hours : 1 });
        },

        async getLatestCrawlCostSum(hours) {
            return post({ action: 'cost_sum', hours: hours != null ? hours : 1 });
        },

        async getDashboardAccountLabel() {
            return post({ action: 'account_label_get' });
        },

        async setDashboardAccountLabel(label) {
            return post({ action: 'account_label_set', label: label != null ? String(label) : '' });
        },
    };

    window.getPywebviewAPI = function () {
        return null;
    };

    window.waitForAPI = async function () {
        return webApi;
    };
})();
