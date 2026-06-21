---
name: beautiful-mermaid
description: |
  Рендерит Mermaid-диаграммы в SVG или ASCII-графику. Используй, когда пользователь присылает Mermaid-блоки
  (\`\`\`mermaid ... \`\`\`) и хочет их визуализировать. Поддерживает flowchart, state, sequence, class и ER-диаграммы.
  Возможности: очень быстрый рендер (100+ диаграмм меньше чем за 500 мс), без DOM-зависимостей, 15 встроенных тем, совместимость с темами Shiki.
  Подходит для Telegram-сообщений, терминального вывода, веб-интерфейсов и CLI-инструментов.
---

# Beautiful Mermaid

Рендерит Mermaid-диаграммы в аккуратные SVG или ASCII-графику. Работает быстро,
полностью настраивается темами и не требует DOM-зависимостей.

## Когда использовать

Используй этот скилл, когда:

- пользователь присылает Mermaid-блоки (\`\`\`mermaid ... \`\`\`);
- пользователь просит "отрендерить" или "визуализировать" диаграмму;
- нужен терминальный или ASCII-вывод диаграммы;
- нужны диаграммы с темой оформления: доступно 15 встроенных тем;
- нужен SVG-вывод для насыщенного интерфейса.

## Установка

```bash
npm install beautiful-mermaid
# или
bun add beautiful-mermaid
# или
pnpm add beautiful-mermaid
```

## Быстрый старт

### SVG-вывод по умолчанию

```typescript
import { renderMermaid } from "beautiful-mermaid";

const svg = await renderMermaid(`
  graph TD
    A[Старт] --> B{Решение}
    B -->|Да| C[Действие]
    B -->|Нет| D[Конец]
`);
```

### ASCII-вывод для терминала

```typescript
import { renderMermaidAscii } from "beautiful-mermaid";

const ascii = renderMermaidAscii(`graph LR; A --> B --> C`);
```

**Вывод:**

```text
┌───┐     ┌───┐     ┌───┐
│   │     │   │     │   │
│ A │────►│ B │────►│ C │
│   │     │   │     │   │
└───┘     └───┘     └───┘
```

## Поддерживаемые диаграммы

| Тип       | Синтаксис           | Описание                            |
| --------- | ------------------- | ----------------------------------- |
| Flowchart | `graph TD/LR/BT/RL` | Поддерживаются все направления      |
| State     | `stateDiagram-v2`   | Диаграммы конечных автоматов        |
| Sequence  | `sequenceDiagram`   | Диаграммы последовательности        |
| Class     | `classDiagram`      | Диаграммы наследования классов      |
| ER        | `erDiagram`         | Диаграммы сущностей и связей        |

### Пример flowchart

\`\`\`mermaid
graph TD
A[Старт] --> B{Решение}
B -->|Да| C[Действие]
B -->|Нет| D[Конец]
C --> D
\`\`\`

### Пример sequence diagram

\`\`\`mermaid
sequenceDiagram
Alice->>Bob: Hello Bob!
Bob-->>Alice: Hi Alice!
\`\`\`

## Система тем

### Встроенные темы: 15 вариантов

```typescript
import { renderMermaid, THEMES } from "beautiful-mermaid";

// Использовать встроенную тему
const svg = await renderMermaid(diagram, THEMES["tokyo-night"]);

// Доступные темы:
THEMES["zinc-light"];
THEMES["zinc-dark"];
THEMES["tokyo-night"];
THEMES["tokyo-night-storm"];
THEMES["tokyo-night-light"];
THEMES["catppuccin-mocha"];
THEMES["catppuccin-latte"];
THEMES["nord"];
THEMES["nord-light"];
THEMES["dracula"];
THEMES["github-light"];
THEMES["github-dark"];
THEMES["solarized-light"];
THEMES["solarized-dark"];
THEMES["one-dark"];
```

### Пользовательская тема в монохромном режиме

```typescript
// Достаточно двух цветов: остальные система выводит сама
const svg = await renderMermaid(diagram, {
  bg: "#1a1b26", // фон
  fg: "#a9b1d6", // основной цвет
});
```

### Расширенная тема

```typescript
const svg = await renderMermaid(diagram, {
  bg: "#1a1b26",
  fg: "#a9b1d6",
  line: "#3d59a1", // цвет ребер
  accent: "#7aa2f7", // стрелки и акценты
  muted: "#565f89", // вторичный текст
  surface: "#292e42", // заливка узлов
  border: "#3d59a1", // обводка узлов
});
```

### Совместимость с темами Shiki

```typescript
import { renderMermaid, fromShikiTheme } from "beautiful-mermaid";
import { getHighlighter } from "shiki";

const highlighter = await getHighlighter({ theme: "vitesse-dark" });
const colors = fromShikiTheme(highlighter.getTheme("vitesse-dark"));
const svg = await renderMermaid(diagram, colors);
```

## ASCII/Unicode-вывод

Для терминальных окружений:

```typescript
import { renderMermaidAscii } from "beautiful-mermaid";

// Unicode: красивее, используется по умолчанию
const unicode = renderMermaidAscii(`graph LR; A --> B`);

// Чистый ASCII: максимальная совместимость
const ascii = renderMermaidAscii(`graph LR; A --> B`, { useAscii: true });

// Пользовательские отступы
renderMermaidAscii(diagram, {
  useAscii: false,
  paddingX: 5, // горизонтальный отступ
  paddingY: 5, // вертикальный отступ
  boxBorderPadding: 1, // внутренний отступ
});
```

## API

### renderMermaid(text, options?): Promise<string>

Рендерит Mermaid в SVG.

**Параметры:**

| Опция         | Тип     | По умолчанию | Описание                 |
| ------------- | ------- | --------- | -------------------------- |
| `bg`          | string  | `#FFFFFF` | Цвет фона                  |
| `fg`          | string  | `#27272A` | Основной цвет              |
| `line`        | string? | -         | Цвет ребер                 |
| `accent`      | string? | -         | Стрелки и акценты          |
| `muted`       | string? | -         | Вторичный текст            |
| `surface`     | string? | -         | Оттенок заливки узлов      |
| `border`      | string? | -         | Обводка узлов              |
| `font`        | string  | `Inter`   | Семейство шрифта           |
| `transparent` | boolean | `false`   | Прозрачный фон             |

### renderMermaidAscii(text, options?): string

Рендерит Mermaid в ASCII/Unicode. Синхронный вызов.

**Параметры:**

| Опция              | Тип     | По умолчанию | Описание                      |
| ------------------ | ------- | ------- | -------------------------------- |
| `useAscii`         | boolean | `false` | Использовать ASCII вместо Unicode |
| `paddingX`         | number  | `5`     | Горизонтальный отступ узлов      |
| `paddingY`         | number  | `5`     | Вертикальный отступ узлов        |
| `boxBorderPadding` | number  | `1`     | Внутренний отступ блока          |

### THEMES: Record<string, DiagramColors>

Все 15 встроенных тем.

### fromShikiTheme(theme): DiagramColors

Извлекает цвета диаграммы из темы Shiki.

## Использование в OpenClaw

### Интеграция с Telegram

Для Telegram рендерь SVG и отправляй его как изображение:

```typescript
import { renderMermaid } from "beautiful-mermaid";

async function sendMermaid(message: string) {
  const blocks = extractMermaidBlocks(message);

  for (const block of blocks) {
    const svg = await renderMermaid(block.code, THEMES["tokyo-night"]);
    // Отправить SVG как изображение в Telegram
  }
}
```

### Вывод в терминал или CLI

```typescript
import { renderMermaidAscii } from "beautiful-mermaid";

function printDiagram(code: string) {
  const ascii = renderMermaidAscii(code);
  console.log(ascii);
}
```

## Производительность

- **100+ диаграмм** меньше чем за 500 мс.
- **Без DOM-зависимостей** - чистый TypeScript.
- **Очень быстро** - браузер и Puppeteer не нужны.

## Сравнение с альтернативами

| Возможность | beautiful-mermaid        | mmdc      |
| ----------- | ------------------------ | --------- |
| Зависимости | Без DOM                  | Puppeteer |
| Скорость    | <500 мс для 100+ диаграмм | Медленнее |
| ASCII       | ✅ Нативно                | ❌ Нет    |
| Темы        | 15 встроенных + Shiki     | CSS       |
| Размер      | Легковесный              | Тяжелый   |

## Пример рабочего процесса

**Ввод:**

```text
Вот архитектура системы:

\`\`\`mermaid
graph TD
  Пользователь --> LB[Балансировщик]
  LB --> API[API-сервер]
  API --> DB[(База данных)]
  API --> Cache
\`\`\`

И поток вызовов:

\`\`\`mermaid
sequenceDiagram
  participant U as Пользователь
  participant A as API
  U->>A: Запрос
  A-->>U: Ответ
\`\`\`
```

**Действие:** отрендерить обе диаграммы с подходящей темой.

**Вывод:** отправить два SVG-изображения с подписями.

## Ресурсы

- **npm:** https://www.npmjs.com/package/beautiful-mermaid
- **GitHub:** https://github.com/lukilabs/beautiful-mermaid
- **Живая демонстрация:** https://agents.craft.do/mermaid
