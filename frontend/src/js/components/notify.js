document.addEventListener('DOMContentLoaded', function () {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = protocol + '//' + window.location.host + '/ws/notify';
    let socket;
    const badge = document.getElementById('notification-badge');
    const itemsContainer = document.getElementById('notification-items');
    const noMsg = document.getElementById('no-notifications-msg');

    // Create toast container if it doesn't exist
    let toastContainer = document.querySelector('.toast.toast-top.toast-end');
    if (!toastContainer) {
        toastContainer = document.createElement('div');
        toastContainer.className = 'toast toast-top toast-end';
        document.body.appendChild(toastContainer);
    }

    const dismissChannel = new BroadcastChannel('vchat_toast_dismiss');

    // Apply custom styles as requested
    toastContainer.style.cssText = "position: fixed; z-index: 10000; width: 300px; top: 5rem; right: 1rem; max-height: calc(-2rem + 100vh); overflow-y: auto;";

    if (!badge || !itemsContainer) {
        // Even if notification dropdown is invalid, we still want toasts to work?
        // The original code returned early. Let's try to proceed if toastContainer exists, 
        // but maybe we should separate the logic.
        // For now, let's keep the return check but maybe we shouldn't if we want generic toasts.
        // However, the user said "flash calls... send to redis... come as notifications... via websocket_handler".
        // websocket_handler is `@login_required`.
        // If the user is logged in, these elements should likely exist if the layout is standard.
        // If not, we might be on a page without the navbar?
        // Let's assume standard layout.
        if (!badge || !itemsContainer) {
            // If we want to support toasts even without the dropdown (e.g. minimal layout), 
            // we should likely remove this return or verify if badge exists.
            // But existing code returns. Let's respect it for now to avoid side effects, 
            // or maybe relax it? User said "flash... send... displayed...". 
            // If I return here, flash won't work on pages without notification badge.
            // I'll proceed only if badge/itemsContainer logic is conditional, or simply modify the check.
            // Let's modify the check to only return if we can't do EITHER.
            if (!badge && !itemsContainer && !toastContainer) return;
        }
    }

    function dismissToast(mid) {
        const el = toastContainer.querySelector(`[data-mid="${CSS.escape(mid)}"]`);
        if (el) el.remove();
    }

    dismissChannel.onmessage = function (event) {
        if (event.data?.type === 'dismiss' && event.data.mid) {
            dismissToast(event.data.mid);
        }
    };

    function showToast(data) {
        const mid = data.mid || data.created_at || String(Date.now());
        // Don't show the same toast twice in this tab (e.g. if somehow delivered twice)
        if (toastContainer.querySelector(`[data-mid="${CSS.escape(mid)}"]`)) return;

        const alertClass = {
            'error': 'alert-error',
            'success': 'alert-success',
            'warning': 'alert-warning',
            'info': 'alert-info'
        }[data.category] || 'alert-info';

        const div = document.createElement('div');
        div.className = `alert ${alertClass} mb-2 cursor-pointer`;
        div.dataset.mid = mid;
        div.innerHTML = `<span>${data.body}</span>`;
        div.onclick = function () {
            div.remove();
            dismissChannel.postMessage({ type: 'dismiss', mid });
        };

        toastContainer.appendChild(div);

        // Limit to 5 messages
        const toasts = toastContainer.children;
        if (toasts.length > 5) {
            toasts[0].remove();
        }
    }

    function connect() {
        socket = new WebSocket(wsUrl);

        socket.onopen = function () {};

        socket.onmessage = function (event) {
            const data = JSON.parse(event.data);

            if (data.type === 'flash') {
                showToast(data);
                return;
            }

            // Normal notification handling
            if (badge) badge.classList.remove('hidden');

            // Hide "no messages" placeholder
            if (noMsg) noMsg.style.display = 'none';

            function timeAgo(dateString) {
                if (!dateString) return 'Только что';
                const date = new Date(dateString);
                if (isNaN(date.getTime())) return 'Только что';

                const now = new Date();
                const seconds = Math.floor((now - date) / 1000);

                const intervals = [
                    ['год', 'года', 'лет', 31536000],
                    ['месяц', 'месяца', 'месяцев', 2592000],
                    ['день', 'дня', 'дней', 86400],
                    ['час', 'часа', 'часов', 3600],
                    ['минута', 'минуты', 'минут', 60],
                ];

                const pluralize = (n, one, few, many) => {
                    const mod10 = n % 10;
                    const mod100 = n % 100;
                    if (mod10 === 1 && mod100 !== 11) return one;
                    if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return few;
                    return many;
                };

                for (const [one, few, many, secondsInUnit] of intervals) {
                    const interval = Math.floor(seconds / secondsInUnit);
                    if (interval >= 1) {
                        return `${interval} ${pluralize(interval, one, few, many)} назад`;
                    }
                }
                return 'Только что';
            }

            // Create item HTML
            const itemHtml = `
            <div class="hover:bg-base-200/20 relative flex items-start gap-3 p-4 transition-all border-b border-base-200 border-dashed last:border-0">
              <div class="grow">
                <p class="text-sm leading-tight">${data.body}</p>
                 <p class="text-base-content/60 text-xs">${timeAgo(data.created_at)}</p>
              </div>
            </div>`;

            // Insert after header (which is index 0)
            if (itemsContainer) {
                // If container has header (first child), insert after it. 
                // Wait, original code said "Insert after header (which is index 0)". 
                // Let's verify structure. Assuming firstElementChild is header.
                const header = itemsContainer.firstElementChild;
                if (header) {
                    header.insertAdjacentHTML('afterend', itemHtml);
                } else {
                    itemsContainer.insertAdjacentHTML('afterbegin', itemHtml);
                }
            }
        };

        socket.onclose = function (e) {
            console.log('Notification WS closed', e.reason);
            // Reconnect after delay
            setTimeout(connect, 5000);
        };

        socket.onerror = function (err) {
            console.error('Notification WS error: ', err);
            socket.close();
        };
    }

    connect();
});
