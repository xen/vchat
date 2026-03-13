import hljs from '../vendor/highlight/core.js';
import javascript from '../vendor/highlight/languages/javascript.js';
import php from '../vendor/highlight/languages/php.js';
import xml from '../vendor/highlight/languages/xml.js';

hljs.registerLanguage('javascript', javascript);
hljs.registerLanguage('php', php);
hljs.registerLanguage('xml', xml);

const highlightBlocks = () => {
  const codeBlocks = document.querySelectorAll('[data-code-block] code');
  codeBlocks.forEach((block) => hljs.highlightElement(block));
};

const copyBlocks = () => {
  const copyButtons = document.querySelectorAll('[data-copy-target]');
  copyButtons.forEach((button) => {
    button.addEventListener('click', () => {
      const targetId = button.getAttribute('data-copy-target');
      const codeBlock = document.getElementById(targetId);
      if (!codeBlock) {
        return;
      }

      const textToCopy = codeBlock.textContent.trim();
      const copiedLabel = button.getAttribute('data-copied-label') || 'Copied!';
      const labelElement = button.querySelector('.copy-label');
      const originalLabel = button.dataset.originalLabel || (labelElement ? labelElement.textContent.trim() : button.textContent.trim());
      button.dataset.originalLabel = originalLabel;

      const onCopied = () => {
        if (labelElement) {
          labelElement.textContent = copiedLabel;
        } else {
          button.textContent = copiedLabel;
        }
        setTimeout(() => {
          if (labelElement) {
            labelElement.textContent = originalLabel;
          } else {
            button.textContent = originalLabel;
          }
        }, 1800);
      };

      const fallbackCopy = () => {
        const textarea = document.createElement('textarea');
        textarea.value = textToCopy;
        textarea.style.position = 'fixed';
        textarea.style.left = '-9999px';
        document.body.appendChild(textarea);
        textarea.select();
        try {
          document.execCommand('copy');
          onCopied();
        } catch (err) {
          console.error('Copy failed', err);
        }
        document.body.removeChild(textarea);
      };

      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(textToCopy).then(onCopied).catch(fallbackCopy);
      } else {
        fallbackCopy();
      }
    });
  });
};

document.addEventListener('DOMContentLoaded', () => {
  highlightBlocks();
  copyBlocks();
});

export default hljs;
