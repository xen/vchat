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

    toastContainer.style.cssText = "position: fixed; z-index: 10000; width: 300px; top: 5rem; right: 1rem;";

    if (!badge && !itemsContainer && !toastContainer) return;

    const PEEK = 8;       // px each older toast peeks below the top one
    const MAX_VISIBLE = 3;

    function updateStack() {
        const toasts = Array.from(toastContainer.children);
        const n = toasts.length;
        if (n === 0) {
            toastContainer.style.height = '0';
            return;
        }
        const topH = toasts[n - 1].offsetHeight;
        toasts.forEach((toast, i) => {
            const depth = n - 1 - i; // 0 = newest/top
            const clamped = Math.min(depth, MAX_VISIBLE - 1);
            toast.style.position = 'absolute';
            toast.style.top = '0';
            toast.style.left = '0';
            toast.style.right = '0';
            toast.style.margin = '0';
            toast.style.transition = 'transform 0.2s ease, opacity 0.2s ease';
            toast.style.transform = `translateY(${clamped * PEEK}px) scale(${1 - clamped * 0.04})`;
            toast.style.zIndex = depth < MAX_VISIBLE ? String(MAX_VISIBLE - depth) : '0';
            toast.style.opacity = depth === 0 ? '1' : depth === 1 ? '0.65' : depth < MAX_VISIBLE ? '0.35' : '0';
            toast.style.pointerEvents = depth === 0 ? 'auto' : 'none';
        });
        const layers = Math.min(n, MAX_VISIBLE);
        toastContainer.style.height = topH + (layers - 1) * PEEK + 'px';
    }

    function dismissToast(mid) {
        const el = toastContainer.querySelector(`[data-mid="${CSS.escape(mid)}"]`);
        if (el) {
            el.remove();
            requestAnimationFrame(updateStack);
        }
    }

    dismissChannel.onmessage = function (event) {
        if (event.data?.type === 'dismiss' && event.data.mid) {
            dismissToast(event.data.mid);
        }
    };

    function showToast(data) {
        const mid = data.mid || data.created_at || String(Date.now());
        if (toastContainer.querySelector(`[data-mid="${CSS.escape(mid)}"]`)) return;

        const alertClass = {
            'error': 'alert-error',
            'success': 'alert-success',
            'warning': 'alert-warning',
            'info': 'alert-info'
        }[data.category] || 'alert-info';

        const div = document.createElement('div');
        div.className = `alert ${alertClass} cursor-pointer`;
        div.dataset.mid = mid;
        div.innerHTML = `<span>${data.body}</span>`;
        div.onclick = function () {
            div.remove();
            dismissChannel.postMessage({ type: 'dismiss', mid });
            requestAnimationFrame(updateStack);
        };

        // Drop oldest if already at limit
        while (toastContainer.children.length >= MAX_VISIBLE + 2) {
            toastContainer.children[0].remove();
        }

        toastContainer.appendChild(div);
        requestAnimationFrame(updateStack);
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
