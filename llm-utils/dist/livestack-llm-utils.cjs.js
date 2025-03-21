'use strict';

if (process.env.NODE_ENV === "production") {
  module.exports = require("./livestack-llm-utils.cjs.prod.js");
} else {
  module.exports = require("./livestack-llm-utils.cjs.dev.js");
}
