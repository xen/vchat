#! /usr/bin/env node

// Local pandoc filter for Mermaid diagrams (DOCX-focused):
// - render with narrower mmdc width (encourages vertical layout)
// - force C4 diagrams to top-down layout
// - set Pandoc image attribute width (default 17cm) for stable DOCX sizing

var fs = require('fs');
var path = require('path');
var exec = require('child_process').execSync;
var process = require('process');

// This repo keeps JS deps under frontend/node_modules.
// When pandoc executes this filter from the repo root, Node won't find modules
// next to this script, so we resolve them explicitly.
function requireFromFrontend(moduleName) {
  return require(path.resolve(__dirname, '..', 'frontend', 'node_modules', moduleName));
}

var pandoc = requireFromFrontend('pandoc-filter');
var tmp = requireFromFrontend('tmp');
var sanfile = requireFromFrontend('sanitize-filename');

var prefix = 'diagram';
var cmd = externalTool('mmdc');
var counter = 0;
var folder = process.cwd();

// Redirect stderr to a file; otherwise pandoc can hang on non-JSON output.
var errFile = path.join(folder, 'mermaid-filter.err');
var errorLog = fs.createWriteStream(errFile);

function mermaid(type, value, format, meta) {
  if (type != 'CodeBlock') return null;

  var attrs = value[0];
  var content = value[1];

  var id = attrs[0];
  var classes = attrs[1];

  var options = {
    width: process.env.MERMAID_FILTER_WIDTH || 400,
    format: process.env.MERMAID_FILTER_FORMAT || 'png',
    loc: process.env.MERMAID_FILTER_LOC || 'inline',
    theme: process.env.MERMAID_FILTER_THEME || 'default',
    background: process.env.MERMAID_FILTER_BACKGROUND || 'white',
    caption: process.env.MERMAID_FILTER_CAPTION || '',
    filename: process.env.MERMAID_FILTER_FILENAME || '',
    scale: process.env.MERMAID_FILTER_SCALE || 3,
    imageClass: process.env.MERMAID_FILTER_IMAGE_CLASS || '',

    // Critical: give DOCX/Word an explicit diagram width.
    // A value below 100% helps keep diagrams readable without dominating the page.
    imageWidth: process.env.MERMAID_FILTER_IMAGE_WIDTH || '72%'
  };

  var configFile =
    process.env.MERMAID_FILTER_MERMAID_CONFIG ||
    path.join(folder, '.mermaid-config.json');
  var confFileOpts = '';
  if (fs.existsSync(configFile)) {
    confFileOpts += ` -c "${configFile}"`;
  }

  var puppeteerConfig =
    process.env.MERMAID_FILTER_PUPPETEER_CONFIG || path.join(folder, '.puppeteer.json');
  var puppeteerOpts = '';
  if (fs.existsSync(puppeteerConfig)) {
    puppeteerOpts += ` -p "${puppeteerConfig}"`;
  }

  var cssFile =
    process.env.MERMAID_FILTER_MERMAID_CSS || path.join(folder, '.mermaid.css');
  if (fs.existsSync(cssFile)) {
    confFileOpts += ` -C "${cssFile}"`;
  }

  if (classes.indexOf('mermaid') < 0) return null;

  attrs[2].map((item) => {
    if (item.length === 1) options[item[0]] = true;
    else options[item[0]] = item[1];
  });

  counter++;

  // Encourage vertical/portrait diagrams:
  // - keep a narrow render width (options.width)
  // - use `.mermaid-config.json` to force C4 "one column" layout (c4ShapeInRow=1)
  // - optionally rewrite flowchart direction to top-down (single column-friendly)
  var forceVerticalRaw = String(
    process.env.MERMAID_FILTER_FORCE_VERTICAL === undefined
      ? '1'
      : process.env.MERMAID_FILTER_FORCE_VERTICAL
  ).toLowerCase();
  var forceVertical =
    forceVerticalRaw === '1' || forceVerticalRaw === 'true' || forceVerticalRaw === 'yes';
  var forceC4OneColumnRaw = String(
    process.env.MERMAID_FILTER_FORCE_C4_ONE_COLUMN === undefined
      ? '1'
      : process.env.MERMAID_FILTER_FORCE_C4_ONE_COLUMN
  ).toLowerCase();
  var forceC4OneColumn =
    forceC4OneColumnRaw === '1' ||
    forceC4OneColumnRaw === 'true' ||
    forceC4OneColumnRaw === 'yes';

  var renderedContent = content;
  if (forceVertical) {
    renderedContent = renderedContent.replace(
      /^(\s*)(graph|flowchart)\s+(LR|RL|BT)\b/m,
      '$1$2 TD'
    );
  }
  if (forceC4OneColumn && /^\s*C4(?:Context|Container|Component|Dynamic|Deployment)\b/m.test(renderedContent)) {
    if (!/^\s*UpdateLayoutConfig\s*\(/m.test(renderedContent)) {
      renderedContent = renderedContent.replace(
        /^(\s*C4(?:Context|Container|Component|Dynamic|Deployment)\b.*)$/m,
        '$1\n    UpdateLayoutConfig($c4ShapeInRow="1", $c4BoundaryInRow="1")'
      );
    }
  }

  var tmpfileObj = tmp.fileSync();
  fs.writeFileSync(tmpfileObj.name, renderedContent);

  var outdir = options.loc !== 'imgur' ? options.loc : path.dirname(tmpfileObj.name);

  if (options.caption !== '' && options.filename === '') {
    options.filename = sanfile(options.caption, { replacement: '' }).replace(
      /[#$~%+;()\[\]{}&=_\-\s]/g,
      ''
    );
  }

  if (options.filename === '') {
    options.filename = `${prefix}-${counter}`;
  }

  var savePath = tmpfileObj.name + '.' + options.format;

  var fullCmd = `${cmd} ${confFileOpts} ${puppeteerOpts} -w ${options.width} -s ${options.scale} -f -i "${tmpfileObj.name}" -t ${options.theme} -b ${options.background} -o "${savePath}"`;
  exec(fullCmd);

  var newPath = path.join(outdir, `${options.filename}.${options.format}`);
  if (options.loc == 'inline') {
    if (options.format === 'svg') {
      var dataSvg = fs.readFileSync(savePath, 'utf8');
      newPath = 'data:image/svg+xml;base64,' + Buffer.from(dataSvg).toString('base64');
    } else if (options.format === 'pdf') {
      newPath = savePath;
    } else {
      var dataPng = fs.readFileSync(savePath);
      newPath = 'data:image/png;base64,' + Buffer.from(dataPng).toString('base64');
    }
  } else if (options.loc === 'imgur') {
    var imgur = externalTool('imgur');
    newPath = exec(`${imgur} ${savePath}`).toString().trim().replace('http://', 'https://');
  } else {
    mv(savePath, newPath);
  }

  var fig = options.caption !== '' ? 'fig:' : '';

  var imageClasses = options.imageClass ? [options.imageClass] : [];
  var imageKeyvals = [];
  if (options.imageWidth) {
    imageKeyvals.push(['width', String(options.imageWidth)]);
  }

  return pandoc.Para([
    pandoc.Image([id, imageClasses, imageKeyvals], [pandoc.Str(options.caption)], [newPath, fig])
  ]);
}


function externalTool(command) {
  var paths = [
    path.resolve(__dirname, '..', 'frontend', 'node_modules', '.bin', command),
    path.resolve(__dirname, 'node_modules', '.bin', command),
    path.resolve(__dirname, '..', 'node_modules', '.bin', command)
  ];

  var envCmdName =
    'MERMAID_FILTER_CMD_' + (command || '').toUpperCase().replace(/[^A-Z0-9-]/g, '_');
  var envCmd = process.env[envCmdName];
  if (envCmd) {
    paths = [envCmd];
    command = 'env: ' + envCmdName;
  }

  return firstExisting(paths, function () {
    console.error('External tool not found: ' + command);
    process.exit(1);
  });
}

function mv(from, to) {
  var readStream = fs.createReadStream(from);
  var writeStream = fs.createWriteStream(to);

  readStream.on('close', () => {
    fs.unlinkSync(from);
  });
  readStream.pipe(writeStream);
}

function firstExisting(paths, error) {
  for (var i = 0; i < paths.length; i++) {
    if (fs.existsSync(paths[i])) return `"${paths[i]}"`;
  }
  error();
}

(async function () {
  process.stderr.write = errorLog.write.bind(errorLog);
  await pandoc.toJSONFilter(function (item, format, meta) {
    return mermaid(item.t, item.c, format, meta);
  });
})().catch(function (error) {
  console.error(error && error.stack ? error.stack : error);
  process.exit(1);
});
