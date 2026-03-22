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

  // Create Iframe Container
  var iframeContainer = document.createElement("div");
  iframeContainer.id = "vchat-widget-iframe-container";
  document.body.appendChild(iframeContainer);

  // Toggle Logic
  var isOpen = false;
  var iframeLoaded = false;

  button.addEventListener("click", function () {
    isOpen = !isOpen;
    if (isOpen) {
      iframeContainer.style.display = "flex";
      button.innerHTML =
        '<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg>';

      if (!iframeLoaded) {
        var iframe = document.createElement("iframe");
        iframe.id = "vchat-widget-iframe";
        var origin = new URL(src).origin;
        var chatPath = {{ widget_chat_path | tojson | safe }};
        var chatUrl = new URL(origin + chatPath);
        if (userUid) chatUrl.searchParams.append("user_uid", userUid);
        if (userName) chatUrl.searchParams.append("user_name", userName);
        if (userEmail) chatUrl.searchParams.append("user_email", userEmail);
        if (sign) chatUrl.searchParams.append("sign", sign);

        iframe.src = chatUrl.toString();
        iframeContainer.appendChild(iframe);
        iframeLoaded = true;
      }
    } else {
      iframeContainer.style.display = "none";
      button.innerHTML =
        '<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"></path></svg>';
    }
  });
})();
