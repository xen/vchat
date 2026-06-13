import { Marked } from "marked";

const SAFE_LINK_PROTOCOLS = new Set(["http:", "https:", "mailto:"]);

const citationExtension = {
    name: "citation",
    level: "inline",
    start(src) {
        return src.match(/\[\[citation:/)?.index;
    },
    tokenizer(src) {
        const rule = /^\[\[citation:(\d+)\]\]/;
        const match = rule.exec(src);
        if (!match) {
            return;
        }
        return {
            type: "citation",
            raw: match[0],
            id: match[1],
        };
    },
};

const safeMarked = new Marked({ gfm: true, breaks: false });
safeMarked.use({ extensions: [citationExtension] });

function escapeHtml(value) {
    return String(value ?? "")
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#39;");
}

function safeHref(value) {
    try {
        const url = new URL(String(value ?? ""), window.location.href);
        if (!SAFE_LINK_PROTOCOLS.has(url.protocol)) {
            return null;
        }
        return url.href;
    } catch {
        return null;
    }
}

function textFromToken(token) {
    if (!token) {
        return "";
    }
    if (typeof token.raw === "string") {
        return token.raw;
    }
    if (typeof token.text === "string") {
        return token.text;
    }
    return "";
}

function renderInline(tokens = []) {
    return tokens.map(renderInlineToken).join("");
}

function renderInlineToken(token) {
    switch (token.type) {
        case "text":
        case "escape":
            return escapeHtml(token.text ?? token.raw ?? "");
        case "strong":
            return `<strong>${renderInline(token.tokens)}</strong>`;
        case "em":
            return `<em>${renderInline(token.tokens)}</em>`;
        case "codespan":
            return `<code>${escapeHtml(token.text ?? "")}</code>`;
        case "br":
            return "<br>";
        case "link": {
            const label = renderInline(token.tokens);
            const href = safeHref(token.href);
            if (!href) {
                return label || escapeHtml(token.raw ?? token.text ?? "");
            }
            return `<a href="${escapeHtml(href)}" target="_blank" rel="noopener noreferrer">${label}</a>`;
        }
        case "citation": {
            const id = String(token.id ?? "");
            if (!/^\d+$/.test(id)) {
                return escapeHtml(token.raw ?? "");
            }
            const citationNumber = Number.parseInt(id, 10) + 1;
            return `<button type="button" class="citation-btn" data-id="${id}" aria-label="Открыть источник ${citationNumber}">${citationNumber}</button>`;
        }
        case "html":
        case "image":
            return escapeHtml(textFromToken(token));
        default:
            return escapeHtml(textFromToken(token));
    }
}

function renderList(token) {
    const tag = token.ordered ? "ol" : "ul";
    const items = (token.items || [])
        .map((item) => `<li>${renderBlocks(item.tokens || [])}</li>`)
        .join("");
    return `<${tag}>${items}</${tag}>`;
}

function renderBlocks(tokens = []) {
    return tokens.map(renderBlockToken).join("");
}

function renderBlockToken(token) {
    switch (token.type) {
        case "space":
            return "";
        case "paragraph":
            return `<p>${renderInline(token.tokens)}</p>`;
        case "text":
            if (Array.isArray(token.tokens)) {
                return renderInline(token.tokens);
            }
            return escapeHtml(token.text ?? token.raw ?? "");
        case "code": {
            const lang = String(token.lang || "").match(/^[a-zA-Z0-9_-]+$/)
                ? ` class="language-${escapeHtml(token.lang)}"`
                : "";
            return `<pre><code${lang}>${escapeHtml(token.text ?? "")}</code></pre>`;
        }
        case "list":
            return renderList(token);
        case "html":
            return `<p>${escapeHtml(textFromToken(token))}</p>`;
        case "heading":
        case "blockquote":
        case "table":
        case "hr":
        case "def":
            return `<p>${escapeHtml(textFromToken(token))}</p>`;
        default:
            return `<p>${escapeHtml(textFromToken(token))}</p>`;
    }
}

export function renderSafeAssistantMarkdown(value) {
    const text = String(value ?? "");
    const tokens = safeMarked.lexer(text);
    return renderBlocks(tokens);
}
