import "./chat.css";
import "htmx.org/dist/htmx.min.js";
import { marked } from "marked";
window.marked = marked;

// Custom renderer for citations
const renderer = new marked.Renderer();
const originalLink = renderer.link;

// Override paragraph to handle standalone citations if needed, or just standard text processing
// But for inline citations, we might need a custom extension or just a post-processing step.
// Since marked doesn't easily support arbitrary custom syntax without extensions,
// let's use a simple regex replacement on the output or a custom extension.

// Let's use marked.use with an extension for [[citation:ID]]
const citationExtension = {
    name: 'citation',
    level: 'inline',
    start(src) { return src.match(/\[\[citation:/)?.index; },
    tokenizer(src, tokens) {
        const rule = /^\[\[citation:(\d+)\]\]/;
        const match = rule.exec(src);
        if (match) {
            return {
                type: 'citation',
                raw: match[0],
                id: match[1]
            };
        }
    },
    renderer(token) {
        return `<button class="inline-flex items-center justify-center w-4 h-4 ml-0.5 text-[0.6rem] font-bold text-primary bg-primary/10 rounded-full align-top cursor-pointer hover:bg-primary hover:text-primary-content transition-colors citation-btn" data-id="${token.id}">${parseInt(token.id) + 1}</button>`;
    }
};

marked.use({ extensions: [citationExtension] });

document.addEventListener('click', (e) => {
    if (e.target.matches('.citation-btn')) {
        const id = e.target.dataset.id;
        // Find the source in the source list (checking global sources or DOM)
        // We need to implement highlighting logic.
        // Dispatch a custom event or call a function if available.
        const event = new CustomEvent('citation-click', { detail: { id } });
        document.dispatchEvent(event);

        // Simple fallback highlighting for now
        const sourceElement = document.querySelector(`.source-item[data-id="${id}"]`);
        if (sourceElement) {
            sourceElement.scrollIntoView({ behavior: 'smooth', block: 'center' });
            sourceElement.classList.add('ring', 'ring-primary');
            setTimeout(() => sourceElement.classList.remove('ring', 'ring-primary'), 2000);
        }
    }
});
