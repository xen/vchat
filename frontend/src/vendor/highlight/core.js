const languages = {};

const escapeHtml = (text) =>
  text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');

const createHighlighter = (rules) => (code) => {
  let result = '';
  let index = 0;

  while (index < code.length) {
    let matched = false;

    for (const rule of rules) {
      rule.regex.lastIndex = index;
      const match = rule.regex.exec(code);

      if (match && match.index === index) {
        const value = match[0];
        const escaped = escapeHtml(value);
        result += `<span class="${rule.className}">${escaped}</span>`;
        index += value.length;
        matched = true;
        break;
      }
    }

    if (!matched) {
      result += escapeHtml(code[index]);
      index += 1;
    }
  }

  return result;
};

const registerLanguage = (name, languageFactory) => {
  if (typeof languageFactory !== 'function') {
    return;
  }
  const language = languageFactory({ createHighlighter, escapeHtml });
  if (language && typeof language.highlight === 'function') {
    languages[name] = language.highlight;
  }
};

const highlightElement = (element) => {
  if (!element) {
    return;
  }

  const languageClass = Array.from(element.classList).find((cls) => cls.startsWith('language-'));
  const languageName = languageClass ? languageClass.replace('language-', '') : null;
  const code = element.textContent || '';

  const highlighter = languageName ? languages[languageName] : null;
  const html = highlighter ? highlighter(code) : escapeHtml(code);

  element.innerHTML = html;
  element.classList.add('hljs');
};

const core = { registerLanguage, highlightElement };

export { registerLanguage, highlightElement };
export default core;
