import "./chat.css";
import "htmx.org/dist/htmx.min.js";
import FingerprintJS from "@fingerprintjs/fingerprintjs";
import { renderSafeAssistantMarkdown } from "./safe-markdown.js";

window.vchatRenderAssistantMarkdown = renderSafeAssistantMarkdown;

let fingerprintAgentPromise = null;

function fallbackHash(value) {
    let hash = 0;
    for (let index = 0; index < value.length; index += 1) {
        hash = (hash << 5) - hash + value.charCodeAt(index);
        hash |= 0;
    }
    return `fallback-${Math.abs(hash)}`;
}

async function buildFallbackFingerprint() {
    const seed = [
        navigator.userAgent || "",
        navigator.language || "",
        navigator.platform || "",
        `${window.screen?.width || 0}x${window.screen?.height || 0}`,
        Intl.DateTimeFormat().resolvedOptions().timeZone || "",
    ].join("|");
    return fallbackHash(seed);
}

window.vchatGetFingerprint = async function vchatGetFingerprint() {
    if (!fingerprintAgentPromise) {
        fingerprintAgentPromise = FingerprintJS.load().catch(() => null);
    }

    let visitorId = "";
    try {
        const fp = await fingerprintAgentPromise;
        if (fp) {
            const result = await fp.get();
            visitorId = result.visitorId || "";
        }
    } catch {
        visitorId = "";
    }

    if (!visitorId) {
        visitorId = await buildFallbackFingerprint();
    }

    return {
        device_fingerprint: visitorId,
        platform:
            navigator.userAgentData?.platform ||
            navigator.platform ||
            "",
        language: navigator.language || "",
        timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || "",
        screen: `${window.screen?.width || 0}x${window.screen?.height || 0}`,
    };
};

document.addEventListener("click", (event) => {
    const target = event.target;
    if (!(target instanceof HTMLElement) || !target.matches(".citation-btn")) {
        return;
    }

    const bubble = target.closest(".chat-bubble");
    const toggle = bubble?.querySelector("[data-structured-context='true'] button");
    if (toggle instanceof HTMLElement) {
        toggle.click();
    }
});
