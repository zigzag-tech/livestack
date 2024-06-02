// rollup.config.mjs

import typescript from "@rollup/plugin-typescript";

export default {
  input: "src/index.ts",
  output: {
    format: "cjs",
    file: "dist/index.js",
    sourcemap: true,
  },
  plugins: [
    typescript({
      sourceMap: true,
      inlineSources: true,
    }),
  ],
};
