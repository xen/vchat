# Codex handoff — Mermaid вертикально/читабельно в DOCX

Цель: сделать Mermaid-вставки в DOCX (Word) читаемыми и «портретными» (вертикальная компоновка).

## Что уже сделано

1) Добавлен локальный pandoc-фильтр для Mermaid, чтобы:
- рендерить диаграммы в более узком полотне (это стимулирует вертикальную компоновку);
- вставлять картинку в DOCX с атрибутом `width=100%`, чтобы Word масштабировал её по ширине страницы.

Файл: `bin/mermaid-filter-fit.js`

2) Добавлен Mermaid config, который подхватывается mermaid-cli (`mmdc`) через `-c .mermaid-config.json`:
- увеличен общий размер шрифта;
- для C4 включено «в 1 колонку» через `c4ShapeInRow: 1` и `c4BoundaryInRow: 1`.

Файл: `.mermaid-config.json`

3) `make docs` переключён на локальный фильтр и выставлены дефолтные env-параметры:
- `MERMAID_FILTER_WIDTH=420` (узкий рендер, чаще делает диаграмму выше)
- `MERMAID_FILTER_IMAGE_WIDTH=100%` (важно для Word)

Файл: `Makefile` (target `docs`)

## Как воспроизвести

Из корня репо:

- `make docs`

Результат: `docs/word/*.docx`

## Как тюнить под конкретный вид

### Сделать ещё «вертикальнее»
- Уменьшить `MERMAID_FILTER_WIDTH` (например 360–420)
- Увеличить `flowchart.rankSpacing`/`flowchart.nodeSpacing` в `.mermaid-config.json`

### Сделать крупнее текст
- Увеличить `themeVariables.fontSize` в `.mermaid-config.json` (например `24px`)

### Если диаграммы стали слишком «узкие»
- Поднять `MERMAID_FILTER_WIDTH` (например 500–650)
- Оставить `MERMAID_FILTER_IMAGE_WIDTH=100%`, чтобы Word всё равно растягивал.

## Контекст и ограничения

- Фильтр `bin/mermaid-filter-fit.js` специально резолвит зависимости из `frontend/node_modules/*`, потому что JS-зависимости установлены там.
- `make docs` использует `PUPPETEER_EXECUTABLE_PATH` (как было раньше) для стабильного запуска mermaid-cli.

## Быстрая проверка результата (опционально)

Можно распаковать DOCX и посмотреть, что у картинок задана ширина через Pandoc/Word разметку (extent), и что диаграммы действительно растягиваются.

