export default function ({ escapeHtml }) {
  const highlightAttributes = (content) => {
    const attrRegex = /([A-Za-z_:][\w:.-]*)(\s*=\s*("[^"]*"|'[^']*'|[^\s>]+))?/g;
    let highlighted = '';
    let match;

    while ((match = attrRegex.exec(content)) !== null) {
      const [, name, valuePart = ''] = match;
      highlighted += ` <span class="hljs-attr">${escapeHtml(name)}</span>`;

      const value = valuePart.trim();
      if (value) {
        const quote = value[0];
        if (quote === '"' || quote === "'") {
          const inner = value.slice(1, -1);
          highlighted += `=${quote}<span class="hljs-string">${escapeHtml(inner)}</span>${quote}`;
        } else {
          highlighted += `=<span class="hljs-string">${escapeHtml(value)}</span>`;
        }
      }
    }

    return highlighted;
  };

  const highlightTag = (tagText) => {
    const isClosing = tagText.startsWith('</');
    const isSelfClosing = tagText.endsWith('/>');
    const nameMatch = /^<\/?\s*([^\s/>]+)/.exec(tagText);
    const name = nameMatch ? nameMatch[1] : '';
    const attributesPart = tagText
      .replace(/^<\/?\s*[^\s/>]+/, '')
      .replace(/\/?\s*>$/, '')
      .trim();

    const start = isClosing ? '</' : '<';
    const end = isSelfClosing ? '/>' : '>';

    let result = `<span class="hljs-tag">${escapeHtml(start)}</span>`;
    if (name) {
      result += `<span class="hljs-name">${escapeHtml(name)}</span>`;
    }
    if (attributesPart) {
      result += highlightAttributes(attributesPart);
    }
    result += `<span class="hljs-tag">${escapeHtml(end)}</span>`;

    return result;
  };

  const highlight = (code) => {
    let result = '';
    let index = 0;

    while (index < code.length) {
        if (code.startsWith('<!--', index)) {
          const end = code.indexOf('-->', index + 4);
          const endIndex = end === -1 ? code.length : end + 3;
          const comment = code.slice(index, endIndex);
          result += `<span class="hljs-comment">${escapeHtml(comment)}</span>`;
          index = endIndex;
          continue;
        }

        if (code[index] === '<') {
          const end = code.indexOf('>', index + 1);
          const endIndex = end === -1 ? code.length : end + 1;
          const tagText = code.slice(index, endIndex);
          result += highlightTag(tagText);
          index = endIndex;
          continue;
        }

        result += escapeHtml(code[index]);
        index += 1;
    }

    return result;
  };

  return { highlight };
}
