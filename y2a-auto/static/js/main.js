// 主JavaScript文件
(function installCsrfProtection() {
    const token = document.querySelector('meta[name="csrf-token"]')?.content || '';
    if (!token) return;

    const originalFetch = window.fetch.bind(window);
    window.fetch = function(input, init) {
        const options = Object.assign({}, init || {});
        const requestMethod = input instanceof Request ? input.method : 'GET';
        const method = String(options.method || requestMethod || 'GET').toUpperCase();
        const requestUrl = new URL(
            typeof input === 'string' ? input : input.url,
            window.location.href
        );
        if (
            requestUrl.origin === window.location.origin
            && !['GET', 'HEAD', 'OPTIONS', 'TRACE'].includes(method)
        ) {
            const headers = new Headers(options.headers || {});
            if (!headers.has('X-CSRF-Token')) headers.set('X-CSRF-Token', token);
            options.headers = headers;
        }
        return originalFetch(input, options);
    };

    document.addEventListener('DOMContentLoaded', function() {
        document.querySelectorAll('form').forEach(function(form) {
            const method = String(form.method || 'get').toLowerCase();
            const action = new URL(form.action || window.location.href, window.location.href);
            if (
                method === 'get'
                || action.origin !== window.location.origin
                || form.querySelector('input[name="_csrf_token"]')
            ) return;
            const input = document.createElement('input');
            input.type = 'hidden';
            input.name = '_csrf_token';
            input.value = token;
            form.appendChild(input);
        });
    });
}());

document.addEventListener('DOMContentLoaded', function() {
    console.log('PotatoFlow 已加载');

    // --- 设置页面的密码保护逻辑 ---
    const settingsForm = document.querySelector('form[method="post"][enctype="multipart/form-data"]');
    if (settingsForm) {
        const newPassword = document.getElementById('new_password');
        const confirmPassword = document.getElementById('confirm_password');
        const passwordError = document.getElementById('password-match-error');
        const passwordProtectionEnabled = document.getElementById('password_protection_enabled');
        const passwordFields = document.getElementById('password-fields');

        if (passwordProtectionEnabled) {
            function togglePasswordFields() {
                if (passwordProtectionEnabled.checked) {
                    passwordFields.style.display = 'block';
                } else {
                    passwordFields.style.display = 'none';
                }
            }

            // Initial state
            togglePasswordFields();
            passwordProtectionEnabled.addEventListener('change', togglePasswordFields);
        }

        settingsForm.addEventListener('submit', function(event) {
            // 仅在启用密码保护时才校验密码匹配
            if (passwordProtectionEnabled && passwordProtectionEnabled.checked &&
                newPassword && confirmPassword && newPassword.value !== confirmPassword.value) {
                event.preventDefault(); // 阻止表单提交
                if (passwordError) {
                    passwordError.classList.remove('d-none');
                }
            } else {
                if (passwordError) {
                    passwordError.classList.add('d-none');
                }
            }
        });
    }


    // --- 设置页面的日志清理按钮逻辑 ---
    // 绑定手动日志清理按钮
    const manualCleanupBtn = document.getElementById('manual-cleanup-btn');
    const logCleanupHoursField = document.getElementById('log-cleanup-hours');
    const cleanupHoursHidden = document.getElementById('cleanup-hours-input');
    if(manualCleanupBtn) {
        manualCleanupBtn.addEventListener('click', async function() {
            // 使用当前输入的小时数（若存在）
            if (logCleanupHoursField && cleanupHoursHidden) {
                const hours = parseInt(logCleanupHoursField.value, 10);
                if (!isNaN(hours) && hours > 0) {
                    cleanupHoursHidden.value = hours;
                }
            }
            const confirmMsg = `确定要手动清理旧日志吗？将删除 ${cleanupHoursHidden ? cleanupHoursHidden.value : ''} 小时前的日志文件。`;
            if (await window.PotatoUI.confirm(confirmMsg, {title: '清理旧日志', confirmText: '开始清理', danger: true})) {
                const form = document.getElementById('cleanup-form');
                if (form) form.submit();
            }
        });
    }

    // 绑定立即清空日志按钮
    const clearLogsBtn = document.getElementById('clear-logs-btn');
    const confirmClearBtn = document.getElementById('confirm-clear-btn');
    const cancelClearBtn = document.getElementById('cancel-clear-btn');
    const clearWarning = document.getElementById('clear-warning');

    if (clearLogsBtn) {
        clearLogsBtn.addEventListener('click', function() {
            clearLogsBtn.classList.add('d-none');
            confirmClearBtn.classList.remove('d-none');
            cancelClearBtn.classList.remove('d-none');
            clearWarning.classList.remove('d-none');
        });
    }

    if (cancelClearBtn) {
        cancelClearBtn.addEventListener('click', function() {
            clearLogsBtn.classList.remove('d-none');
            confirmClearBtn.classList.add('d-none');
            cancelClearBtn.classList.add('d-none');
            clearWarning.classList.add('d-none');
        });
    }
    
    if (confirmClearBtn) {
        confirmClearBtn.addEventListener('click', function() {
             document.getElementById('clear-form').submit();
        });
    }

    // --- 设置页面的下载内容清理按钮逻辑 ---
    // 绑定手动下载内容清理按钮
    const manualDownloadCleanupBtn = document.getElementById('manual-download-cleanup-btn');
    const downloadCleanupHoursField = document.getElementById('download-cleanup-hours');
    const downloadCleanupHoursHidden = document.getElementById('download-cleanup-hours-input');
    if(manualDownloadCleanupBtn) {
        manualDownloadCleanupBtn.addEventListener('click', async function() {
            // 使用当前输入的小时数（若存在）
            if (downloadCleanupHoursField && downloadCleanupHoursHidden) {
                const hours = parseInt(downloadCleanupHoursField.value, 10);
                if (!isNaN(hours) && hours > 0) {
                    downloadCleanupHoursHidden.value = hours;
                }
            }
            const confirmMsg = `确定要手动清理旧的下载内容吗？将删除 ${downloadCleanupHoursHidden ? downloadCleanupHoursHidden.value : ''} 小时前的下载文件和目录。`;
            if (await window.PotatoUI.confirm(confirmMsg, {title: '清理下载内容', confirmText: '开始清理', danger: true})) {
                const form = document.getElementById('download-cleanup-form');
                if (form) form.submit();
            }
        });
    }
});
