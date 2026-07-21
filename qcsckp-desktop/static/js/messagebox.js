/**
 * 自定义 MessageBox，替代原生 alert / confirm（深色主题）。
 *
 * showToast(message, type?, durationMs?) — 顶部浮现提示，自动消失（默认约 2.8s），图标依赖 js/lucide.js（data-lucide）
 * showMsgBox(message, type?) — 与 pywebview 父页约定一致，等同 showToast(type: success|error|info)
 * appAlert(message, title?)  -> Promise<void>  已改为顶部 Toast，不再使用阻塞式弹窗
 * appConfirm(message, title?) -> Promise<boolean>  确定=true，取消=false（仍为模态）
 */
(function () {
    var cssDone = false;
    var overlay = null;
    var titleEl, bodyEl, btnOk, btnCancel;
    var _resolve = null;
    var _mode = 'alert';

    function injectCss() {
        if (cssDone) return;
        cssDone = true;
        var s = document.createElement('style');
        s.textContent =
            '#app-mbox-overlay{position:fixed;inset:0;z-index:200;display:none;' +
            'align-items:center;justify-content:center;padding:16px;' +
            'background:rgba(15,23,42,.78);backdrop-filter:blur(4px);}' +
            '#app-mbox-overlay.app-mbox-visible{display:flex;}' +
            '#app-mbox-panel{width:100%;max-width:400px;border-radius:16px;' +
            'border:1px solid #334155;background:linear-gradient(180deg,#1e293b 0%,#0f172a 100%);' +
            'box-shadow:0 25px 50px -12px rgba(0,0,0,.55);overflow:hidden;}' +
            '#app-mbox-title{font-size:16px;font-weight:700;color:#f8fafc;padding:20px 20px 0;}' +
            '#app-mbox-body{font-size:14px;line-height:1.55;color:#cbd5e1;padding:14px 20px 20px;white-space:pre-wrap;word-break:break-word;}' +
            '#app-mbox-actions{display:flex;justify-content:flex-end;gap:10px;padding:0 20px 20px;flex-wrap:wrap;}' +
            '#app-mbox-actions .app-mbox-btn-cancel{padding:10px 16px;border-radius:8px;border:1px solid #475569;' +
            'background:transparent;color:#cbd5e1;cursor:pointer;font-size:13px;font-family:inherit;}' +
            '#app-mbox-actions .app-mbox-btn-cancel:hover{background:rgba(148,163,184,.12);}' +
            '#app-mbox-actions .app-mbox-btn-ok{padding:10px 20px;border-radius:8px;border:none;' +
            'background:#38bdf8;color:#0f172a;font-weight:600;cursor:pointer;font-size:13px;font-family:inherit;}' +
            '#app-mbox-actions .app-mbox-btn-ok:hover{background:#7dd3fc;}';
        document.head.appendChild(s);
    }

    var toastCssDone = false;
    var toastHost = null;
    var toastTimer = null;

    function injectToastCss() {
        if (toastCssDone) return;
        toastCssDone = true;
        var s = document.createElement('style');
        /* 对齐 static/dashboard.html 顶部 Toast：slate-800 + border-slate-600 + flex + 左侧图标 */
        s.textContent =
            '#app-toast-host{position:fixed;left:50%;top:1rem;z-index:300;' +
            'transform:translateX(-50%);max-width:min(92vw,28rem);pointer-events:none;' +
            'opacity:0;transition:opacity .3s ease;}' +
            '#app-toast-host.app-toast-visible{opacity:1;}' +
            '#app-toast-inner{display:flex;align-items:center;gap:8px;pointer-events:auto;' +
            'background:#1e293b;color:#fff;padding:8px 16px;border-radius:8px;' +
            'border:1px solid #475569;box-shadow:0 10px 15px -3px rgba(0,0,0,.35),0 4px 6px -2px rgba(0,0,0,.2);' +
            'font-size:14px;line-height:1.5;font-weight:500;font-family:inherit;' +
            'white-space:pre-wrap;word-break:break-word;}' +
            '#app-toast-ico{flex-shrink:0;display:flex;align-items:center;justify-content:center;' +
            'width:16px;height:16px;color:inherit;}' +
            '#app-toast-ico .app-toast-lucide{display:block;width:16px;height:16px;}' +
            '#app-toast-ico .app-toast-lucide svg{width:16px;height:16px;display:block;}' +
            '#app-toast-inner.app-toast-success #app-toast-ico{color:#34d399;}' +
            '#app-toast-inner.app-toast-error #app-toast-ico{color:#fb7185;}' +
            '#app-toast-inner.app-toast-warning #app-toast-ico{color:#fbbf24;}' +
            '#app-toast-inner.app-toast-info #app-toast-ico{color:#38bdf8;}';
        document.head.appendChild(s);
    }

    /** Lucide 图标名（与 data-lucide 一致） */
    function lucideNameForToastKind(kind) {
        if (kind === 'success') return 'check-circle';
        if (kind === 'error') return 'frown';
        if (kind === 'warning') return 'alert-triangle';
        return 'info';
    }

    function mountToastLucideIcon(icoEl, kind) {
        var name = lucideNameForToastKind(kind);
        icoEl.innerHTML =
            '<i data-lucide="' + name + '" class="app-toast-lucide" aria-hidden="true"></i>';
        var L = window.lucide;
        if (L && typeof L.createIcons === 'function') {
            L.createIcons();
        }
    }

    function ensureToastDom() {
        injectToastCss();
        if (toastHost) return;
        toastHost = document.createElement('div');
        toastHost.id = 'app-toast-host';
        toastHost.setAttribute('role', 'status');
        toastHost.setAttribute('aria-live', 'polite');
        toastHost.innerHTML =
            '<div id="app-toast-inner" class="app-toast-row app-toast-info">' +
            '<span id="app-toast-ico" aria-hidden="true"></span>' +
            '<span id="app-toast-text"></span>' +
            '</div>';
        document.body.appendChild(toastHost);
    }

    function mapToastType(t) {
        if (t === 'success' || t === 'error' || t === 'warning' || t === 'info') return t;
        return 'info';
    }

    window.showToast = function (message, type, durationMs) {
        ensureToastDom();
        var inner = document.getElementById('app-toast-inner');
        var textEl = document.getElementById('app-toast-text');
        var icoEl = document.getElementById('app-toast-ico');
        if (!inner || !textEl || !icoEl) return;
        var kind = mapToastType(type);
        textEl.textContent = message == null ? '' : String(message);
        inner.className = 'app-toast-row app-toast-' + kind;
        mountToastLucideIcon(icoEl, kind);
        if (toastTimer) clearTimeout(toastTimer);
        toastHost.classList.remove('app-toast-visible');
        void toastHost.offsetWidth;
        toastHost.classList.add('app-toast-visible');
        var d = typeof durationMs === 'number' && durationMs > 0 ? durationMs : 2800;
        toastTimer = setTimeout(function () {
            toastHost.classList.remove('app-toast-visible');
            toastTimer = null;
        }, d);
    };

    /** 与 gui / pywebview 内嵌页约定：第二参数为 success | error | info */
    window.showMsgBox = function (message, type) {
        window.showToast(message, mapToastType(type), 2800);
    };

    function ensureDom() {
        injectCss();
        if (overlay) return;
        overlay = document.createElement('div');
        overlay.id = 'app-mbox-overlay';
        overlay.setAttribute('role', 'dialog');
        overlay.setAttribute('aria-modal', 'true');
        overlay.setAttribute('aria-hidden', 'true');
        overlay.innerHTML =
            '<div id="app-mbox-panel">' +
            '<div id="app-mbox-title"></div>' +
            '<div id="app-mbox-body"></div>' +
            '<div id="app-mbox-actions">' +
            '<button type="button" class="app-mbox-btn-cancel" id="app-mbox-cancel">取消</button>' +
            '<button type="button" class="app-mbox-btn-ok" id="app-mbox-ok">确定</button>' +
            '</div></div>';
        document.body.appendChild(overlay);
        titleEl = document.getElementById('app-mbox-title');
        bodyEl = document.getElementById('app-mbox-body');
        btnOk = document.getElementById('app-mbox-ok');
        btnCancel = document.getElementById('app-mbox-cancel');

        btnOk.addEventListener('click', function () {
            if (!_resolve) return;
            var r = _resolve;
            _resolve = null;
            hide();
            if (_mode === 'confirm') r(true);
            else r();
        });
        btnCancel.addEventListener('click', function () {
            if (!_resolve || _mode !== 'confirm') return;
            var r = _resolve;
            _resolve = null;
            hide();
            r(false);
        });
        overlay.addEventListener('click', function (e) {
            if (e.target !== overlay) return;
            if (_mode === 'confirm' && _resolve) {
                var r = _resolve;
                _resolve = null;
                hide();
                r(false);
            }
        });
        document.addEventListener('keydown', function (e) {
            if (!overlay.classList.contains('app-mbox-visible')) return;
            if (e.key !== 'Escape') return;
            if (!_resolve) return;
            var r = _resolve;
            _resolve = null;
            hide();
            if (_mode === 'confirm') r(false);
            else r();
        });
    }

    function hide() {
        overlay.classList.remove('app-mbox-visible');
        overlay.setAttribute('aria-hidden', 'true');
    }

    function show() {
        overlay.classList.add('app-mbox-visible');
        overlay.setAttribute('aria-hidden', 'false');
        try {
            btnOk.focus();
        } catch (err) {}
    }

    window.appAlert = function (message, title) {
        var text = message == null ? '' : String(message);
        var ti = title == null ? '' : String(title);
        var line =
            ti && ti !== '提示' && ti !== '错误' ? ti + '：' + text : text;
        var kind = ti === '错误' || ti === '无法启动' ? 'error' : 'info';
        window.showToast(line, kind, 2800);
        return Promise.resolve();
    };

    window.appConfirm = function (message, title) {
        ensureDom();
        _mode = 'confirm';
        titleEl.textContent = title || '确认';
        bodyEl.textContent = message == null ? '' : String(message);
        btnCancel.style.display = '';
        return new Promise(function (resolve) {
            _resolve = resolve;
            show();
        });
    };
})();
