import assert from "node:assert/strict";
import test from "node:test";

globalThis.window = {
    location: {
        href: "https://vchat.test/widget",
    },
};

const { renderSafeAssistantMarkdown } = await import("./safe-markdown.js");

test("escapes raw html and script content as text", () => {
    const rendered = renderSafeAssistantMarkdown(
        "<script>alert('I am evil')</script><img src=x onerror=alert(1)>\n\n<div>block</div>",
    );

    assert.equal(rendered.includes("<script>"), false);
    assert.equal(rendered.includes("<img"), false);
    assert.equal(rendered.includes("<div>"), false);
    assert.equal(rendered.includes("onerror"), true);
    assert.equal(rendered.includes("&lt;script&gt;"), true);
    assert.equal(rendered.includes("&lt;img src=x onerror=alert(1)&gt;"), true);
    assert.equal(rendered.includes("&lt;div&gt;block&lt;/div&gt;"), true);
});

test("keeps the allowed markdown subset", () => {
    const rendered = renderSafeAssistantMarkdown(
        "**Жирный** и *курсив* с `кодом`\n\n- пункт\n\n```js\nconst ok = true;\n```",
    );

    assert.match(rendered, /<strong>Жирный<\/strong>/);
    assert.match(rendered, /<em>курсив<\/em>/);
    assert.match(rendered, /<code>кодом<\/code>/);
    assert.match(rendered, /<ul><li>пункт<\/li><\/ul>/);
    assert.match(rendered, /<pre><code class="language-js">const ok = true;<\/code><\/pre>/);
});

test("rejects unsafe links and images", () => {
    const rendered = renderSafeAssistantMarkdown(
        "[bad](javascript:alert(1)) [ok](https://example.test/path) ![img](https://example.test/a.png)",
    );

    assert.equal(rendered.includes("javascript:"), false);
    assert.match(rendered, /<p>bad /);
    assert.match(
        rendered,
        /<a href="https:\/\/example\.test\/path" target="_blank" rel="noopener noreferrer">ok<\/a>/,
    );
    assert.equal(rendered.includes("<img"), false);
    assert.match(rendered, /!\[img\]\(https:\/\/example\.test\/a\.png\)/);
});

test("renders citation tokens without accepting arbitrary attributes", () => {
    const rendered = renderSafeAssistantMarkdown("[[citation:2]] [[citation:x]]");

    assert.match(rendered, /class="[^"]*citation-btn[^"]*"/);
    assert.match(rendered, /data-id="2"/);
    assert.match(rendered, /aria-label="Открыть источник 3"/);
    assert.match(rendered, /\[\[citation:x\]\]/);
});
