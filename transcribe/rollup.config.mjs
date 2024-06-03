// rollup.config.mjs

import typescript from "@rollup/plugin-typescript";
import dts from "rollup-plugin-dts";

export default [
  {
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
        exclude: ["src/server/*"],
      }),
    ],
  },
  {
    input: "client/dist/client/index.d.ts",
    output: {
      format: "cjs",
      file: "client/index.d.ts",
    },
    plugins: [dts()],
  },
];
