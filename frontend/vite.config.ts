import { defineConfig } from "vite";
import injectHTML from "vite-plugin-html-inject";
import { resolve } from "node:path";
import fs from "node:fs";
import tailwindcss from "@tailwindcss/vite";
import { viteStaticCopy } from "vite-plugin-static-copy";

const getHtmlFiles = (dir: string): string[] => {
  let results: string[] = [];
  const list = fs.readdirSync(dir);
  list.forEach((file) => {
    file = resolve(dir, file);
    const stat = fs.statSync(file);
    if (stat && stat.isDirectory()) {
      if (file.endsWith("/partials")) {
        return;
      }
      results = results.concat(getHtmlFiles(file));
    } else if (file.endsWith(".html")) {
      results.push(file);
    }
  });
  return results;
};

// Normalize the paths to ensure consistent forward slashes
const normalizePath = (path: string) =>
  path.split(resolve(__dirname, "src")).join("").replace(/\\/g, "/");

// Use normalizePath function to process the paths correctly
const htmlFiles = getHtmlFiles("src");
const input = {
  ...Object.fromEntries(
    htmlFiles.map((file) => [
      normalizePath(file).replace(".html", "").replace(/^\//, ""),
      file,
    ]),
  ),
  auth: resolve(__dirname, "src/auth.js"),
  index: resolve(__dirname, "src/index.js"),
};

const removecors = () => {
  return {
    name: "remove-cors",
    transformIndexHtml: {
      order: "post",
      handler(html) {
        return html.replace(/crossorigin\s*/g, "");
      },
    },
  };
};

export default defineConfig({
  base: "",
  server: {
    open: "/",
  },
  publicDir: "../public",
  plugins: [
    injectHTML(),
    removecors(),
    tailwindcss(),
    viteStaticCopy({
      targets: [
        {
          src: resolve(__dirname, "node_modules/monaco-editor/min/vs"),
          dest: "assets/monaco-editor",
        },
      ],
    }),
  ],
  root: "src",
  build: {
    outDir: "../dist",
    emptyOutDir: true,
    rollupOptions: {
      input,
      output: {
        entryFileNames: `assets/[name].js`,
        chunkFileNames: `assets/[name].js`,
        assetFileNames: `assets/[name].[ext]`,
      },
    },
  },
});
