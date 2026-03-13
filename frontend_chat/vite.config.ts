import { defineConfig } from "vite";
import { resolve } from "node:path";
import tailwindcss from "@tailwindcss/vite";

const input = {
  chat: resolve(__dirname, "src/chat.js"),
};

export default defineConfig({
  base: "/static/chat/", // Set base to match where it will be served from
  server: {
    open: "/",
  },
  plugins: [tailwindcss()],
  root: "src",
  optimizeDeps: {},
  build: {
    outDir: "../dist", // Output to separate chat directory in dist
    emptyOutDir: true,
    rollupOptions: {
      input,
      output: {
        entryFileNames: `[name].js`, // No assets/ prefix
        chunkFileNames: `[name].js`,
        assetFileNames: `[name].[ext]`, // No assets/ prefix
      },
    },
  },
});
