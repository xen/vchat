export default function ({ createHighlighter }) {
  const keywordPattern = '\\b(await|break|case|catch|class|const|continue|debugger|default|delete|do|else|export|extends|finally|for|function|if|import|in|instanceof|let|new|return|super|switch|this|throw|try|typeof|var|void|while|with|yield)\\b';
  const literalPattern = '\\b(true|false|null|undefined)\\b';

  const rules = [
    { regex: /\/\*[\s\S]*?\*\//y, className: 'hljs-comment' },
    { regex: /\/\/[^\n]*/y, className: 'hljs-comment' },
    { regex: /`(?:[^`\\]|\\.|\n)*`/y, className: 'hljs-string' },
    { regex: /"(?:[^"\\]|\\.)*"/y, className: 'hljs-string' },
    { regex: /'(?:[^'\\]|\\.)*'/y, className: 'hljs-string' },
    { regex: /\b0x[0-9a-fA-F]+\b|\b\d+(?:\.\d+)?\b/y, className: 'hljs-number' },
    { regex: new RegExp(keywordPattern, 'y'), className: 'hljs-keyword' },
    { regex: new RegExp(literalPattern, 'y'), className: 'hljs-literal' },
  ];

  return {
    highlight: createHighlighter(rules),
  };
}
