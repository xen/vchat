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
    console.log(toastContainer);

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

    function showToast(data) {
        const alertClass = {
            'error': 'alert-error',
            'success': 'alert-success',
            'warning': 'alert-warning',
            'info': 'alert-info'
        }[data.category] || 'alert-info';

        const div = document.createElement('div');
        div.className = `alert ${alertClass} relative pr-10 mb-2`; // basic spacing
        div.innerHTML = `
            <span>${data.body}</span>
            <button class="btn btn-sm btn-ghost btn-circle absolute right-1 top-1" aria-label="Close">
                <svg xmlns="http://www.w3.org/2000/svg" class="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" /></svg>
            </button>
        `;

        // Close button logic
        const btn = div.querySelector('button');
        btn.onclick = function () {
            div.remove();
        };

        // Append to container
        toastContainer.appendChild(div);

        // Limit to 5 messages
        const toasts = toastContainer.children;
        if (toasts.length > 5) {
            // Remove the oldest (first child)
            toasts[0].remove();
        }

        // Auto-remove after some time? User didn't ask for auto-close, only "if > 5... old ones disappear". 
        // "If user doesn't close them... old should disappear".
    }

    function connect() {
        socket = new WebSocket(wsUrl);

        socket.onopen = function () {
            console.log('Notification WS connected');
        };

        socket.onmessage = function (event) {
            const data = JSON.parse(event.data);
            console.log('Notification received:', data);

            if (data.type === 'flash') {
                showToast(data);
                return;
            }

            // Normal notification handling
            if (badge) badge.classList.remove('hidden');

            // Hide "no messages" placeholder
            if (noMsg) noMsg.style.display = 'none';

            function timeAgo(dateString) {
                if (!dateString) return 'Just now';
                const date = new Date(dateString);
                if (isNaN(date.getTime())) return 'Just now';

                const now = new Date();
                const seconds = Math.floor((now - date) / 1000);

                const intervals = {
                    year: 31536000,
                    month: 2592000,
                    day: 86400,
                    hour: 3600,
                    minute: 60
                };

                for (const [unit, secondsInUnit] of Object.entries(intervals)) {
                    const interval = Math.floor(seconds / secondsInUnit);
                    if (interval >= 1) {
                        return interval + ' ' + unit + (interval > 1 ? 's' : '') + ' ago';
                    }
                }
                return 'Just now';
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
