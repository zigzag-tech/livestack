// rollup.config.js

import typescript from "@rollup/plugin-typescript";
import { nodeResolve } from "@rollup/plugin-node-resolve";
import commonjs from "@rollup/plugin-commonjs";
import babel from 'rollup-plugin-babel';

export default {
  input: "src/index.ts",
  output: {
    format: "cjs",
    file: "lib/index.js",
    sourcemap: true,
  },
  plugins: [
    // nodeResolve(),
    typescript({
      sourceMap: true,
      inlineSources: true,
    }),
    // commonjs({}),
    // babel({}),
  ],
};
