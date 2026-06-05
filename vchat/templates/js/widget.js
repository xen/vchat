(function () {
  // Get script location
  var scriptTag = document.currentScript;
  var src = scriptTag.src;

  // Get configuration from the container div
  var container = document.getElementById("vchat-chat");
  if (!container) {
    console.error("vchat Chat: Container #vchat-chat not found.");
    return;
  }

  var userUid = container.getAttribute("data-user-uid");
  var userName = container.getAttribute("data-user-name") || "";
  var userEmail = container.getAttribute("data-user-email") || "";
  var sign = container.getAttribute("data-xsign") || "";
  var sourcePageUrl = container.getAttribute("data-source-page-url") || window.location.href;
  var triggerResolvePath = {{ trigger_resolve_path | tojson | safe }};
  var triggerItems = [];
  var activeTrigger = null;
  var triggerShown = false;
  var hasScrolled = false;
  var showByTimeout = false;

  // Create Styles
  var style = document.createElement("style");
  style.innerHTML = `
        #vchat-widget-button {
            position: fixed;
            bottom: 20px;
            right: 20px;
            width: 60px;
            height: 60px;
            border-radius: 30px;
            background-color: #000;
            color: #fff;
            border: none;
            box-shadow: 0 4px 12px rgba(0, 0, 0, 0.15);
            cursor: pointer;
            z-index: 9999;
            display: flex;
            align-items: center;
            justify-content: center;
            transition: transform 0.3s ease;
        }
        #vchat-widget-button:hover {
            transform: scale(1.05);
        }
        #vchat-widget-trigger {
            position: fixed;
            bottom: 92px;
            right: 20px;
            max-width: min(320px, calc(100vw - 40px));
            border: none;
            border-radius: 10px;
            background: #fff;
            color: #111;
            box-shadow: 0 8px 24px rgba(0, 0, 0, 0.18);
            cursor: pointer;
            z-index: 9998;
            display: none;
            padding: 12px 14px;
            font: 500 14px/1.35 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
            text-align: left;
            animation: vchat-trigger-in 0.28s ease-out;
        }
        #vchat-widget-trigger::after {
            content: "";
            position: absolute;
            right: 22px;
            bottom: -8px;
            width: 16px;
            height: 16px;
            background: #fff;
            transform: rotate(45deg);
            box-shadow: 4px 4px 10px rgba(0, 0, 0, 0.06);
        }
        #vchat-widget-trigger:hover {
            transform: translateY(-1px);
        }
        @keyframes vchat-trigger-in {
            from { opacity: 0; transform: translateY(8px) scale(0.98); }
            to { opacity: 1; transform: translateY(0) scale(1); }
        }
        #vchat-widget-iframe-container {
            position: fixed;
            bottom: 90px;
            right: 20px;
            width: 380px;
            height: 600px;
            max-height: calc(100vh - 110px);
            background: #fff;
            border-radius: 12px;
            box-shadow: 0 5px 20px rgba(0, 0, 0, 0.15);
            z-index: 9999;
            display: none;
            overflow: hidden;
            flex-direction: column;
        }
        #vchat-widget-iframe {
            width: 100%;
            height: 100%;
            border: none;
        }
        @media (max-width: 480px) {
            #vchat-widget-iframe-container {
                width: 100%;
                height: 100%;
                bottom: 0;
                right: 0;
                border-radius: 0;
                max-height: 100vh;
            }
            #vchat-widget-button {
                bottom: 20px;
                right: 20px;
            }
        }
    `;
  document.head.appendChild(style);

  // Create Button
  var button = document.createElement("button");
  button.id = "vchat-widget-button";
  button.innerHTML =
    '<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"></path></svg>';
  button.title = "Ask AI Support";
  document.body.appendChild(button);

  var triggerButton = document.createElement("button");
  triggerButton.id = "vchat-widget-trigger";
  triggerButton.type = "button";
  document.body.appendChild(triggerButton);

  // Create Iframe Container
  var iframeContainer = document.createElement("div");
  iframeContainer.id = "vchat-widget-iframe-container";
  document.body.appendChild(iframeContainer);

  // Toggle Logic
  var isOpen = false;
  var iframeLoaded = false;
  var iframeEl = null;

  function widgetOrigin() {
    return new URL(src).origin;
  }

  function ensureIframe() {
    if (iframeLoaded && iframeEl) {
      return iframeEl;
    }
    iframeEl = document.createElement("iframe");
    iframeEl.id = "vchat-widget-iframe";
    var chatPath = {{ widget_chat_path | tojson | safe }};
    var chatUrl = new URL(widgetOrigin() + chatPath);
    if (userUid) chatUrl.searchParams.append("user_uid", userUid);
    if (userName) chatUrl.searchParams.append("user_name", userName);
    if (userEmail) chatUrl.searchParams.append("user_email", userEmail);
    if (sign) chatUrl.searchParams.append("sign", sign);
    chatUrl.searchParams.append("source_page_url", sourcePageUrl);

    iframeEl.src = chatUrl.toString();
    iframeContainer.appendChild(iframeEl);
    iframeLoaded = true;
    return iframeEl;
  }

  function openWidget() {
    isOpen = true;
    iframeContainer.style.display = "flex";
    button.innerHTML =
      '<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg>';
    ensureIframe();
  }

  function closeWidget() {
    isOpen = false;
    iframeContainer.style.display = "none";
    button.innerHTML =
      '<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"></path></svg>';
  }

  function canShowTrigger() {
    return hasScrolled || showByTimeout;
  }

  function maybeShowTrigger() {
    if (triggerShown || isOpen || !canShowTrigger() || !triggerItems.length) {
      return;
    }
    activeTrigger = triggerItems[Math.floor(Math.random() * triggerItems.length)];
    triggerButton.textContent = activeTrigger.text;
    triggerButton.style.display = "block";
    triggerShown = true;
  }

  function loadTriggers() {
    var resolveUrl = new URL(widgetOrigin() + triggerResolvePath);
    resolveUrl.searchParams.append("url", sourcePageUrl);
    resolveUrl.searchParams.append("title", document.title || "");
    fetch(resolveUrl.toString(), { mode: "cors", credentials: "omit" })
      .then(function (response) {
        if (!response.ok) throw new Error("Trigger resolve failed");
        return response.json();
      })
      .then(function (payload) {
        triggerItems = Array.isArray(payload.triggers) ? payload.triggers : [];
        if (payload.page_token) {
          triggerItems = triggerItems.map(function (trigger) {
            return Object.assign({}, trigger, { page_token: payload.page_token });
          });
        }
        window.setTimeout(function () {
          showByTimeout = true;
          maybeShowTrigger();
        }, 20000);
        window.setTimeout(maybeShowTrigger, 2500);
      })
      .catch(function () {});
  }

  button.addEventListener("click", function () {
    if (!isOpen) {
      triggerButton.style.display = "none";
      openWidget();
    } else {
      closeWidget();
    }
  });

  triggerButton.addEventListener("click", function () {
    if (!activeTrigger) return;
    triggerButton.style.display = "none";
    openWidget();
    var payload = {
      type: "vchat_trigger",
      page_token: activeTrigger.page_token || null,
      trigger_key: activeTrigger.key || null,
      text: activeTrigger.text
    };
    var targetIframe = ensureIframe();
    window.setTimeout(function () {
      if (targetIframe.contentWindow) {
        targetIframe.contentWindow.postMessage(payload, widgetOrigin());
      }
    }, 500);
  });

  window.addEventListener("scroll", function () {
    if (!hasScrolled && window.scrollY > 160) {
      hasScrolled = true;
      maybeShowTrigger();
    }
  }, { passive: true });

  loadTriggers();
})();
