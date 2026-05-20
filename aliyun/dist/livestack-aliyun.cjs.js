'use strict';

if (process.env.NODE_ENV === "production") {
  module.exports = require("./livestack-aliyun.cjs.prod.js");
} else {
  module.exports = require("./livestack-aliyun.cjs.dev.js");
}
