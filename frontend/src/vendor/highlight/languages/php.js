export default function ({ createHighlighter }) {
  const keywordPattern = '\\b(abstract|and|array|as|break|callable|case|catch|class|clone|const|continue|declare|default|do|echo|else|elseif|enddeclare|endfor|endforeach|endif|endswitch|endwhile|extends|final|finally|for|foreach|function|global|goto|if|implements|include|include_once|instanceof|interface|isset|list|namespace|new|or|print|private|protected|public|require|require_once|return|static|switch|throw|trait|try|unset|use|var|while|xor|yield)\\b';

  const rules = [
    { regex: /<\?(?:php)?|\?>/y, className: 'hljs-tag' },
    { regex: /\/\*[\s\S]*?\*\//y, className: 'hljs-comment' },
    { regex: /\#.*|\/\/[^\n]*/y, className: 'hljs-comment' },
    { regex: /"(?:[^"\\]|\\.)*"/y, className: 'hljs-string' },
    { regex: /'(?:[^'\\]|\\.)*'/y, className: 'hljs-string' },
    { regex: /\$[A-Za-z_][\w]*/y, className: 'hljs-variable' },
    { regex: /\b0x[0-9a-fA-F]+\b|\b\d+(?:\.\d+)?\b/y, className: 'hljs-number' },
    { regex: new RegExp(keywordPattern, 'y'), className: 'hljs-keyword' },
  ];

  return {
    highlight: createHighlighter(rules),
  };
}
