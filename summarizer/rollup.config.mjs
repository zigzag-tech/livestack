// rollup.config.mjs

import typescript from "@rollup/plugin-typescript";

export default {
  input: "src/client/index.ts",
  output: {
    format: "cjs",
    file: "client/index.js",
    sourcemap: true,
  },
  plugins: [
    typescript({
      sourceMap: true,
      inlineSources: true,
      exclude: ["src/server/*"]
    }),
  ],
};
