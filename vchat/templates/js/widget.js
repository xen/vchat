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

  var widgetCode = {{ widget_code | tojson | safe }};
  var userInfo = container.getAttribute("data-user-info") || "";
  var userUid = "";
  var sourcePageUrl = container.getAttribute("data-source-page-url") || window.location.href;
  var demoSystemMessages = container.getAttribute("data-demo-system-messages") === "true";
  var triggerResolvePath = {{ trigger_resolve_path | tojson | safe }};
  var chatStorageTtlMs = 30 * 60 * 1000;
  var triggerItems = [];
  var activeTrigger = null;
  var triggerShown = false;
  var hasScrolled = false;
  var showByTimeout = false;

  function storageKey(suffix) {
    return "vchat:" + widgetCode + ":" + suffix;
  }

  function readStorageJson(key) {
    try {
      var value = window.localStorage.getItem(key);
      return value ? JSON.parse(value) : null;
    } catch (error) {
      return null;
    }
  }

  function writeStorageJson(key, value) {
    try {
      window.localStorage.setItem(key, JSON.stringify(value));
    } catch (error) {}
  }

  function removeStorage(key) {
    try {
      window.localStorage.removeItem(key);
    } catch (error) {}
  }

  function readUserUidFromUserInfo() {
    if (!userInfo) {
      return "";
    }
    try {
      var payload = JSON.parse(userInfo);
      return typeof payload.user_uid === "string" ? payload.user_uid.trim() : "";
    } catch (error) {
      return "";
    }
  }

  function getOrCreateUserUid() {
    var configuredUid = readUserUidFromUserInfo();
    if (configuredUid) {
      return configuredUid;
    }
    var guestKey = storageKey("guest_uid");
    var storedGuestUid = "";
    try {
      storedGuestUid = window.localStorage.getItem(guestKey) || "";
    } catch (error) {}
    if (storedGuestUid) {
      return storedGuestUid;
    }
    var generatedUid = "guest_" + Math.random().toString(16).slice(2, 10);
    try {
      window.localStorage.setItem(guestKey, generatedUid);
    } catch (error) {}
    return generatedUid;
  }

  userUid = getOrCreateUserUid();

  function activeChatStorageKey() {
    return storageKey("active_chat:" + userUid);
  }

  function readActiveChat() {
    var key = activeChatStorageKey();
    var value = readStorageJson(key);
    if (!value || !value.signed_chat_id || !value.last_message_at) {
      removeStorage(key);
      return null;
    }
    if (Date.now() - Number(value.last_message_at) > chatStorageTtlMs) {
      removeStorage(key);
      return null;
    }
    return value;
  }

  function saveActiveChat(signedChatId) {
    if (!signedChatId) {
      return;
    }
    writeStorageJson(activeChatStorageKey(), {
      signed_chat_id: signedChatId,
      last_message_at: Date.now()
    });
  }

  // Create Styles
  var style = document.createElement("style");
  style.innerHTML = `
        @font-face {
            font-family: "SB Sans Text";
            src: url("${widgetOrigin()}/static/chat/SBSansText-Regular.ttf") format("truetype");
            font-weight: 400;
            font-style: normal;
            font-display: swap;
        }
        @font-face {
            font-family: "SB Sans Text";
            src: url("${widgetOrigin()}/static/chat/SBSansText-SemiBold.ttf") format("truetype");
            font-weight: 600;
            font-style: normal;
            font-display: swap;
        }
        @font-face {
            font-family: "SB Sans Display";
            src: url("${widgetOrigin()}/static/chat/SBSansDisplay-SemiBold.ttf") format("truetype");
            font-weight: 600;
            font-style: normal;
            font-display: swap;
        }
        #vchat-widget-button {
            position: fixed;
            bottom: 20px;
            right: 20px;
            width: 60px;
            height: 60px;
            border-radius: 30px;
            background: linear-gradient(135deg, #b2eb38 0%, #6beac7 50%, #31c2a7 100%);
            color: #1c4f5f;
            border: 1px solid rgba(255, 255, 255, 0.72);
            box-shadow: 0 16px 34px rgba(28, 79, 95, 0.2);
            cursor: pointer;
            z-index: 9999;
            display: flex;
            align-items: center;
            justify-content: center;
            isolation: isolate;
            transition: transform 0.2s ease, box-shadow 0.2s ease;
        }
        #vchat-widget-button::before,
        #vchat-widget-button::after {
            content: "";
            position: absolute;
            inset: -12px;
            z-index: -1;
            border: 1px solid rgba(49, 194, 167, 0.22);
            border-radius: 9999px;
            opacity: 0;
            pointer-events: none;
            transform: scale(0.72);
        }
        #vchat-widget-button::after {
            inset: -22px;
            border-color: rgba(178, 235, 56, 0.18);
            animation-delay: 1.2s;
        }
        #vchat-widget-button:not(.vchat-widget-open)::before,
        #vchat-widget-button:not(.vchat-widget-open)::after {
            animation: vchat-widget-rings 2.8s ease-out infinite;
        }
        #vchat-widget-button:not(.vchat-widget-open)::after {
            animation-delay: 1.2s;
        }
        #vchat-widget-button:not(.vchat-widget-open) {
            box-shadow:
                0 16px 34px rgba(28, 79, 95, 0.2),
                0 0 0 12px rgba(107, 234, 199, 0.08),
                0 0 0 24px rgba(178, 235, 56, 0.04);
        }
        #vchat-widget-button:hover {
            transform: translateY(-2px) scale(1.03);
        }
        #vchat-widget-button .vchat-widget-icon {
            width: 28px;
            height: 28px;
            overflow: visible;
            transition: opacity 0.16s ease, transform 0.16s ease;
        }
        #vchat-widget-button .vchat-widget-icon-path {
            fill: none;
            stroke: currentColor;
            stroke-width: 2.15;
            stroke-linecap: round;
            stroke-linejoin: round;
            vector-effect: non-scaling-stroke;
            filter: drop-shadow(0 1px 1px rgba(28, 79, 95, 0.16));
        }
        #vchat-widget-button .vchat-widget-icon-dot {
            fill: currentColor;
            transform-origin: center;
            transition: opacity 0.2s ease, transform 0.2s ease;
            filter: drop-shadow(0 1px 1px rgba(28, 79, 95, 0.16));
        }
        #vchat-widget-button:not(.vchat-widget-open):hover {
            box-shadow:
                0 20px 42px rgba(28, 79, 95, 0.26),
                0 0 0 14px rgba(107, 234, 199, 0.14),
                0 0 0 28px rgba(178, 235, 56, 0.08);
        }
        #vchat-widget-button:not(.vchat-widget-open):hover::before,
        #vchat-widget-button:not(.vchat-widget-open):hover::after {
            border-color: rgba(49, 194, 167, 0.36);
            animation-duration: 2.25s;
        }
        #vchat-widget-button:focus-visible,
        #vchat-widget-trigger:focus-visible {
            outline: 3px solid rgba(107, 234, 199, 0.45);
            outline-offset: 3px;
        }
        #vchat-widget-trigger {
            position: fixed;
            bottom: 92px;
            right: 20px;
            max-width: min(320px, calc(100vw - 40px));
            border: 1px solid rgba(49, 194, 167, 0.22);
            border-radius: 16px 16px 6px 16px;
            background: linear-gradient(180deg, #ffffff 0%, #f7fffc 100%);
            color: #333f48;
            box-shadow: 0 18px 42px rgba(28, 79, 95, 0.16);
            cursor: pointer;
            z-index: 9998;
            display: none;
            padding: 14px 16px;
            font: 400 14px/1.35 "SB Sans Text", Verdana, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
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
            background: #f7fffc;
            transform: rotate(45deg);
            border-right: 1px solid rgba(49, 194, 167, 0.18);
            border-bottom: 1px solid rgba(49, 194, 167, 0.18);
            box-shadow: 4px 4px 10px rgba(28, 79, 95, 0.06);
        }
        #vchat-widget-trigger:hover {
            transform: translateY(-1px);
        }
        @keyframes vchat-trigger-in {
            from { opacity: 0; transform: translateY(8px) scale(0.98); }
            to { opacity: 1; transform: translateY(0) scale(1); }
        }
        @keyframes vchat-widget-rings {
            0% { opacity: 0; transform: scale(0.72); }
            18% { opacity: 0.38; }
            72% { opacity: 0.1; }
            100% { opacity: 0; transform: scale(1.42); }
        }
        @media (prefers-reduced-motion: reduce) {
            #vchat-widget-button::before,
            #vchat-widget-button::after {
                animation: none !important;
            }
        }
        #vchat-widget-iframe-container {
            position: fixed;
            bottom: 90px;
            right: 20px;
            width: 380px;
            height: 600px;
            max-height: calc(100vh - 110px);
            background: #fff;
            border: 1px solid rgba(49, 194, 167, 0.2);
            border-radius: 20px;
            box-shadow: 0 24px 60px rgba(28, 79, 95, 0.22);
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
        @media (max-width: 640px) {
            #vchat-widget-iframe-container {
                inset: 0;
                width: 100dvw;
                height: 100dvh;
                bottom: 0;
                right: 0;
                max-height: none;
                border: 0;
                border-radius: 0;
                box-shadow: none;
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
  button.type = "button";
  button.title = "Спросить ассистента";
  button.setAttribute("aria-label", "Открыть чат с ассистентом");
  button.setAttribute("aria-expanded", "false");
  button.setAttribute("aria-controls", "vchat-widget-iframe-container");
  var closedWidgetIconSvg =
    '<svg class="vchat-widget-icon" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" aria-hidden="true"><path class="vchat-widget-icon-path" d="M21 15a2 2 0 0 1-2 2H8l-5 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"></path></svg>';
  button.innerHTML = closedWidgetIconSvg;
  document.body.appendChild(button);

  var triggerButton = document.createElement("button");
  triggerButton.id = "vchat-widget-trigger";
  triggerButton.type = "button";
  triggerButton.setAttribute("aria-label", "Открыть предложенный вопрос в чате");
  document.body.appendChild(triggerButton);

  // Create Iframe Container
  var iframeContainer = document.createElement("div");
  iframeContainer.id = "vchat-widget-iframe-container";
  iframeContainer.setAttribute("role", "dialog");
  iframeContainer.setAttribute("aria-label", "Чат с ассистентом");
  iframeContainer.setAttribute("aria-hidden", "true");
  document.body.appendChild(iframeContainer);

  // Toggle Logic
  var isOpen = false;
  var iframeLoaded = false;
  var iframeEl = null;

  function widgetOrigin() {
    return new URL(src).origin;
  }

  function buildChatUrl() {
    var chatPath = {{ widget_chat_path | tojson | safe }};
    var chatUrl = new URL(widgetOrigin() + chatPath);
    var activeChat = readActiveChat();
    if (userInfo) chatUrl.searchParams.append("user_info", userInfo);
    if (!userInfo && userUid) chatUrl.searchParams.append("guest_uid", userUid);
    if (activeChat) chatUrl.searchParams.append("chat_id", activeChat.signed_chat_id);
    if (demoSystemMessages) chatUrl.searchParams.append("demo_system_messages", "1");
    chatUrl.searchParams.append("source_page_url", sourcePageUrl);
    return chatUrl.toString();
  }

  function ensureIframe() {
    if (iframeLoaded && iframeEl) {
      return iframeEl;
    }
    iframeEl = document.createElement("iframe");
    iframeEl.id = "vchat-widget-iframe";
    iframeEl.title = "Чат с ассистентом";
    iframeEl.src = buildChatUrl();
    iframeContainer.appendChild(iframeEl);
    iframeLoaded = true;
    return iframeEl;
  }

  function refreshIframe() {
    if (!iframeEl) {
      ensureIframe();
      return;
    }
    iframeEl.src = buildChatUrl();
  }

  function openWidget() {
    isOpen = true;
    button.classList.add("vchat-widget-open");
    button.setAttribute("aria-label", "Закрыть чат с ассистентом");
    button.setAttribute("aria-expanded", "true");
    iframeContainer.style.display = "flex";
    iframeContainer.setAttribute("aria-hidden", "false");
    button.innerHTML =
      '<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg>';
    var frame = ensureIframe();
    window.setTimeout(function () {
      try {
        frame.focus();
      } catch (error) {}
    }, 0);
  }

  function closeWidget() {
    isOpen = false;
    button.classList.remove("vchat-widget-open");
    button.setAttribute("aria-label", "Открыть чат с ассистентом");
    button.setAttribute("aria-expanded", "false");
    iframeContainer.style.display = "none";
    iframeContainer.setAttribute("aria-hidden", "true");
    button.innerHTML = closedWidgetIconSvg;
    button.focus();
  }

  window.addEventListener("message", function (event) {
    if (event.origin !== widgetOrigin()) {
      return;
    }
    if (!iframeEl || event.source !== iframeEl.contentWindow) {
      return;
    }
    if (!event.data || event.data.type !== "vchat_close") {
      return;
    }
    closeWidget();
  });

  window.addEventListener("message", function (event) {
    if (event.origin !== widgetOrigin()) {
      return;
    }
    if (!iframeEl || event.source !== iframeEl.contentWindow) {
      return;
    }
    if (!event.data || event.data.type !== "vchat_chat_activity") {
      return;
    }
    saveActiveChat(event.data.signed_chat_id);
  });

  window.addEventListener("message", function (event) {
    if (event.origin !== widgetOrigin()) {
      return;
    }
    if (!iframeEl || event.source !== iframeEl.contentWindow) {
      return;
    }
    if (!event.data || event.data.type !== "vchat_refresh_chat") {
      return;
    }
    refreshIframe();
  });

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

  window.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && isOpen) {
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
