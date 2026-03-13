import Uppy from "@uppy/core";
import Dashboard from "@uppy/dashboard";
import Tus from "@uppy/tus";
import ru_RU from '@uppy/locales/lib/ru_RU';

export function createUppy(upload_endpoint, target) {
  // Determine current theme
  const getTheme = () => {
    const theme = document.documentElement.getAttribute('data-theme');
    return theme === 'dark' ? 'dark' : 'light';
  };

  const uppy = new Uppy({
    restrictions: {
      allowedFileTypes: ['.pdf', '.txt', '.md', '.doc', '.docx', '.rtf'],
      minNumberOfFiles: 1,
      maxNumberOfFiles: 10
    }
  });

  uppy.use(Dashboard, {
    width: "100%",
    height: 200,
    inline: true,
    autoProceed: true,
    locale: ru_RU,
    target: target,
    showSelectedFiles: false,
    showProgressDetails: true,
    proudlyDisplayPoweredByUppy: false,
    theme: getTheme(),
  })
    .use(Tus, {
      chunkSize: 5242879,
      endpoint: upload_endpoint,
      limit: 6,
      resume: true
    });

  // Watch for theme changes
  const observer = new MutationObserver((mutations) => {
    mutations.forEach((mutation) => {
      if (mutation.type === 'attributes' && mutation.attributeName === 'data-theme') {
        const newTheme = getTheme();
        uppy.getPlugin('Dashboard').setOptions({ theme: newTheme });
      }
    });
  });

  observer.observe(document.documentElement, {
    attributes: true,
    attributeFilter: ['data-theme']
  });
}

window.createUppy = createUppy;
