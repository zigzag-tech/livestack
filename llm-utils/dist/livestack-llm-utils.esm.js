import ollama from 'ollama';
import * as fs from 'fs';
import * as path from 'path';
import * as crypto from 'crypto';
import { z } from 'zod';

function _arrayWithHoles(r) {
  if (Array.isArray(r)) return r;
}

function _iterableToArrayLimit(r, l) {
  var t = null == r ? null : "undefined" != typeof Symbol && r[Symbol.iterator] || r["@@iterator"];
  if (null != t) {
    var e,
      n,
      i,
      u,
      a = [],
      f = !0,
      o = !1;
    try {
      if (i = (t = t.call(r)).next, 0 === l) {
        if (Object(t) !== t) return;
        f = !1;
      } else for (; !(f = (e = i.call(t)).done) && (a.push(e.value), a.length !== l); f = !0);
    } catch (r) {
      o = !0, n = r;
    } finally {
      try {
        if (!f && null != t.return && (u = t.return(), Object(u) !== u)) return;
      } finally {
        if (o) throw n;
      }
    }
    return a;
  }
}

function _arrayLikeToArray(r, a) {
  (null == a || a > r.length) && (a = r.length);
  for (var e = 0, n = Array(a); e < a; e++) n[e] = r[e];
  return n;
}

function _unsupportedIterableToArray(r, a) {
  if (r) {
    if ("string" == typeof r) return _arrayLikeToArray(r, a);
    var t = {}.toString.call(r).slice(8, -1);
    return "Object" === t && r.constructor && (t = r.constructor.name), "Map" === t || "Set" === t ? Array.from(r) : "Arguments" === t || /^(?:Ui|I)nt(?:8|16|32)(?:Clamped)?Array$/.test(t) ? _arrayLikeToArray(r, a) : void 0;
  }
}

function _nonIterableRest() {
  throw new TypeError("Invalid attempt to destructure non-iterable instance.\nIn order to be iterable, non-array objects must have a [Symbol.iterator]() method.");
}

function _slicedToArray(r, e) {
  return _arrayWithHoles(r) || _iterableToArrayLimit(r, e) || _unsupportedIterableToArray(r, e) || _nonIterableRest();
}

function _regeneratorRuntime() {
  _regeneratorRuntime = function () {
    return e;
  };
  var t,
    e = {},
    r = Object.prototype,
    n = r.hasOwnProperty,
    o = Object.defineProperty || function (t, e, r) {
      t[e] = r.value;
    },
    i = "function" == typeof Symbol ? Symbol : {},
    a = i.iterator || "@@iterator",
    c = i.asyncIterator || "@@asyncIterator",
    u = i.toStringTag || "@@toStringTag";
  function define(t, e, r) {
    return Object.defineProperty(t, e, {
      value: r,
      enumerable: !0,
      configurable: !0,
      writable: !0
    }), t[e];
  }
  try {
    define({}, "");
  } catch (t) {
    define = function (t, e, r) {
      return t[e] = r;
    };
  }
  function wrap(t, e, r, n) {
    var i = e && e.prototype instanceof Generator ? e : Generator,
      a = Object.create(i.prototype),
      c = new Context(n || []);
    return o(a, "_invoke", {
      value: makeInvokeMethod(t, r, c)
    }), a;
  }
  function tryCatch(t, e, r) {
    try {
      return {
        type: "normal",
        arg: t.call(e, r)
      };
    } catch (t) {
      return {
        type: "throw",
        arg: t
      };
    }
  }
  e.wrap = wrap;
  var h = "suspendedStart",
    l = "suspendedYield",
    f = "executing",
    s = "completed",
    y = {};
  function Generator() {}
  function GeneratorFunction() {}
  function GeneratorFunctionPrototype() {}
  var p = {};
  define(p, a, function () {
    return this;
  });
  var d = Object.getPrototypeOf,
    v = d && d(d(values([])));
  v && v !== r && n.call(v, a) && (p = v);
  var g = GeneratorFunctionPrototype.prototype = Generator.prototype = Object.create(p);
  function defineIteratorMethods(t) {
    ["next", "throw", "return"].forEach(function (e) {
      define(t, e, function (t) {
        return this._invoke(e, t);
      });
    });
  }
  function AsyncIterator(t, e) {
    function invoke(r, o, i, a) {
      var c = tryCatch(t[r], t, o);
      if ("throw" !== c.type) {
        var u = c.arg,
          h = u.value;
        return h && "object" == typeof h && n.call(h, "__await") ? e.resolve(h.__await).then(function (t) {
          invoke("next", t, i, a);
        }, function (t) {
          invoke("throw", t, i, a);
        }) : e.resolve(h).then(function (t) {
          u.value = t, i(u);
        }, function (t) {
          return invoke("throw", t, i, a);
        });
      }
      a(c.arg);
    }
    var r;
    o(this, "_invoke", {
      value: function (t, n) {
        function callInvokeWithMethodAndArg() {
          return new e(function (e, r) {
            invoke(t, n, e, r);
          });
        }
        return r = r ? r.then(callInvokeWithMethodAndArg, callInvokeWithMethodAndArg) : callInvokeWithMethodAndArg();
      }
    });
  }
  function makeInvokeMethod(e, r, n) {
    var o = h;
    return function (i, a) {
      if (o === f) throw Error("Generator is already running");
      if (o === s) {
        if ("throw" === i) throw a;
        return {
          value: t,
          done: !0
        };
      }
      for (n.method = i, n.arg = a;;) {
        var c = n.delegate;
        if (c) {
          var u = maybeInvokeDelegate(c, n);
          if (u) {
            if (u === y) continue;
            return u;
          }
        }
        if ("next" === n.method) n.sent = n._sent = n.arg;else if ("throw" === n.method) {
          if (o === h) throw o = s, n.arg;
          n.dispatchException(n.arg);
        } else "return" === n.method && n.abrupt("return", n.arg);
        o = f;
        var p = tryCatch(e, r, n);
        if ("normal" === p.type) {
          if (o = n.done ? s : l, p.arg === y) continue;
          return {
            value: p.arg,
            done: n.done
          };
        }
        "throw" === p.type && (o = s, n.method = "throw", n.arg = p.arg);
      }
    };
  }
  function maybeInvokeDelegate(e, r) {
    var n = r.method,
      o = e.iterator[n];
    if (o === t) return r.delegate = null, "throw" === n && e.iterator.return && (r.method = "return", r.arg = t, maybeInvokeDelegate(e, r), "throw" === r.method) || "return" !== n && (r.method = "throw", r.arg = new TypeError("The iterator does not provide a '" + n + "' method")), y;
    var i = tryCatch(o, e.iterator, r.arg);
    if ("throw" === i.type) return r.method = "throw", r.arg = i.arg, r.delegate = null, y;
    var a = i.arg;
    return a ? a.done ? (r[e.resultName] = a.value, r.next = e.nextLoc, "return" !== r.method && (r.method = "next", r.arg = t), r.delegate = null, y) : a : (r.method = "throw", r.arg = new TypeError("iterator result is not an object"), r.delegate = null, y);
  }
  function pushTryEntry(t) {
    var e = {
      tryLoc: t[0]
    };
    1 in t && (e.catchLoc = t[1]), 2 in t && (e.finallyLoc = t[2], e.afterLoc = t[3]), this.tryEntries.push(e);
  }
  function resetTryEntry(t) {
    var e = t.completion || {};
    e.type = "normal", delete e.arg, t.completion = e;
  }
  function Context(t) {
    this.tryEntries = [{
      tryLoc: "root"
    }], t.forEach(pushTryEntry, this), this.reset(!0);
  }
  function values(e) {
    if (e || "" === e) {
      var r = e[a];
      if (r) return r.call(e);
      if ("function" == typeof e.next) return e;
      if (!isNaN(e.length)) {
        var o = -1,
          i = function next() {
            for (; ++o < e.length;) if (n.call(e, o)) return next.value = e[o], next.done = !1, next;
            return next.value = t, next.done = !0, next;
          };
        return i.next = i;
      }
    }
    throw new TypeError(typeof e + " is not iterable");
  }
  return GeneratorFunction.prototype = GeneratorFunctionPrototype, o(g, "constructor", {
    value: GeneratorFunctionPrototype,
    configurable: !0
  }), o(GeneratorFunctionPrototype, "constructor", {
    value: GeneratorFunction,
    configurable: !0
  }), GeneratorFunction.displayName = define(GeneratorFunctionPrototype, u, "GeneratorFunction"), e.isGeneratorFunction = function (t) {
    var e = "function" == typeof t && t.constructor;
    return !!e && (e === GeneratorFunction || "GeneratorFunction" === (e.displayName || e.name));
  }, e.mark = function (t) {
    return Object.setPrototypeOf ? Object.setPrototypeOf(t, GeneratorFunctionPrototype) : (t.__proto__ = GeneratorFunctionPrototype, define(t, u, "GeneratorFunction")), t.prototype = Object.create(g), t;
  }, e.awrap = function (t) {
    return {
      __await: t
    };
  }, defineIteratorMethods(AsyncIterator.prototype), define(AsyncIterator.prototype, c, function () {
    return this;
  }), e.AsyncIterator = AsyncIterator, e.async = function (t, r, n, o, i) {
    void 0 === i && (i = Promise);
    var a = new AsyncIterator(wrap(t, r, n, o), i);
    return e.isGeneratorFunction(r) ? a : a.next().then(function (t) {
      return t.done ? t.value : a.next();
    });
  }, defineIteratorMethods(g), define(g, u, "Generator"), define(g, a, function () {
    return this;
  }), define(g, "toString", function () {
    return "[object Generator]";
  }), e.keys = function (t) {
    var e = Object(t),
      r = [];
    for (var n in e) r.push(n);
    return r.reverse(), function next() {
      for (; r.length;) {
        var t = r.pop();
        if (t in e) return next.value = t, next.done = !1, next;
      }
      return next.done = !0, next;
    };
  }, e.values = values, Context.prototype = {
    constructor: Context,
    reset: function (e) {
      if (this.prev = 0, this.next = 0, this.sent = this._sent = t, this.done = !1, this.delegate = null, this.method = "next", this.arg = t, this.tryEntries.forEach(resetTryEntry), !e) for (var r in this) "t" === r.charAt(0) && n.call(this, r) && !isNaN(+r.slice(1)) && (this[r] = t);
    },
    stop: function () {
      this.done = !0;
      var t = this.tryEntries[0].completion;
      if ("throw" === t.type) throw t.arg;
      return this.rval;
    },
    dispatchException: function (e) {
      if (this.done) throw e;
      var r = this;
      function handle(n, o) {
        return a.type = "throw", a.arg = e, r.next = n, o && (r.method = "next", r.arg = t), !!o;
      }
      for (var o = this.tryEntries.length - 1; o >= 0; --o) {
        var i = this.tryEntries[o],
          a = i.completion;
        if ("root" === i.tryLoc) return handle("end");
        if (i.tryLoc <= this.prev) {
          var c = n.call(i, "catchLoc"),
            u = n.call(i, "finallyLoc");
          if (c && u) {
            if (this.prev < i.catchLoc) return handle(i.catchLoc, !0);
            if (this.prev < i.finallyLoc) return handle(i.finallyLoc);
          } else if (c) {
            if (this.prev < i.catchLoc) return handle(i.catchLoc, !0);
          } else {
            if (!u) throw Error("try statement without catch or finally");
            if (this.prev < i.finallyLoc) return handle(i.finallyLoc);
          }
        }
      }
    },
    abrupt: function (t, e) {
      for (var r = this.tryEntries.length - 1; r >= 0; --r) {
        var o = this.tryEntries[r];
        if (o.tryLoc <= this.prev && n.call(o, "finallyLoc") && this.prev < o.finallyLoc) {
          var i = o;
          break;
        }
      }
      i && ("break" === t || "continue" === t) && i.tryLoc <= e && e <= i.finallyLoc && (i = null);
      var a = i ? i.completion : {};
      return a.type = t, a.arg = e, i ? (this.method = "next", this.next = i.finallyLoc, y) : this.complete(a);
    },
    complete: function (t, e) {
      if ("throw" === t.type) throw t.arg;
      return "break" === t.type || "continue" === t.type ? this.next = t.arg : "return" === t.type ? (this.rval = this.arg = t.arg, this.method = "return", this.next = "end") : "normal" === t.type && e && (this.next = e), y;
    },
    finish: function (t) {
      for (var e = this.tryEntries.length - 1; e >= 0; --e) {
        var r = this.tryEntries[e];
        if (r.finallyLoc === t) return this.complete(r.completion, r.afterLoc), resetTryEntry(r), y;
      }
    },
    catch: function (t) {
      for (var e = this.tryEntries.length - 1; e >= 0; --e) {
        var r = this.tryEntries[e];
        if (r.tryLoc === t) {
          var n = r.completion;
          if ("throw" === n.type) {
            var o = n.arg;
            resetTryEntry(r);
          }
          return o;
        }
      }
      throw Error("illegal catch attempt");
    },
    delegateYield: function (e, r, n) {
      return this.delegate = {
        iterator: values(e),
        resultName: r,
        nextLoc: n
      }, "next" === this.method && (this.arg = t), y;
    }
  }, e;
}

function _classCallCheck(a, n) {
  if (!(a instanceof n)) throw new TypeError("Cannot call a class as a function");
}

function _toPrimitive(t, r) {
  if ("object" != typeof t || !t) return t;
  var e = t[Symbol.toPrimitive];
  if (void 0 !== e) {
    var i = e.call(t, r || "default");
    if ("object" != typeof i) return i;
    throw new TypeError("@@toPrimitive must return a primitive value.");
  }
  return ("string" === r ? String : Number)(t);
}

function _toPropertyKey(t) {
  var i = _toPrimitive(t, "string");
  return "symbol" == typeof i ? i : i + "";
}

function _defineProperties(e, r) {
  for (var t = 0; t < r.length; t++) {
    var o = r[t];
    o.enumerable = o.enumerable || !1, o.configurable = !0, "value" in o && (o.writable = !0), Object.defineProperty(e, _toPropertyKey(o.key), o);
  }
}
function _createClass(e, r, t) {
  return r && _defineProperties(e.prototype, r), t && _defineProperties(e, t), Object.defineProperty(e, "prototype", {
    writable: !1
  }), e;
}

function _defineProperty(e, r, t) {
  return (r = _toPropertyKey(r)) in e ? Object.defineProperty(e, r, {
    value: t,
    enumerable: !0,
    configurable: !0,
    writable: !0
  }) : e[r] = t, e;
}

function ownKeys(e, r) {
  var t = Object.keys(e);
  if (Object.getOwnPropertySymbols) {
    var o = Object.getOwnPropertySymbols(e);
    r && (o = o.filter(function (r) {
      return Object.getOwnPropertyDescriptor(e, r).enumerable;
    })), t.push.apply(t, o);
  }
  return t;
}
function _objectSpread2(e) {
  for (var r = 1; r < arguments.length; r++) {
    var t = null != arguments[r] ? arguments[r] : {};
    r % 2 ? ownKeys(Object(t), !0).forEach(function (r) {
      _defineProperty(e, r, t[r]);
    }) : Object.getOwnPropertyDescriptors ? Object.defineProperties(e, Object.getOwnPropertyDescriptors(t)) : ownKeys(Object(t)).forEach(function (r) {
      Object.defineProperty(e, r, Object.getOwnPropertyDescriptor(t, r));
    });
  }
  return e;
}

function asyncGeneratorStep(n, t, e, r, o, a, c) {
  try {
    var i = n[a](c),
      u = i.value;
  } catch (n) {
    return void e(n);
  }
  i.done ? t(u) : Promise.resolve(u).then(r, o);
}
function _asyncToGenerator(n) {
  return function () {
    var t = this,
      e = arguments;
    return new Promise(function (r, o) {
      var a = n.apply(t, e);
      function _next(n) {
        asyncGeneratorStep(a, r, o, _next, _throw, "next", n);
      }
      function _throw(n) {
        asyncGeneratorStep(a, r, o, _next, _throw, "throw", n);
      }
      _next(void 0);
    });
  };
}

function _OverloadYield(e, d) {
  this.v = e, this.k = d;
}

function _wrapAsyncGenerator(e) {
  return function () {
    return new AsyncGenerator(e.apply(this, arguments));
  };
}
function AsyncGenerator(e) {
  var r, t;
  function resume(r, t) {
    try {
      var n = e[r](t),
        o = n.value,
        u = o instanceof _OverloadYield;
      Promise.resolve(u ? o.v : o).then(function (t) {
        if (u) {
          var i = "return" === r ? "return" : "next";
          if (!o.k || t.done) return resume(i, t);
          t = e[i](t).value;
        }
        settle(n.done ? "return" : "normal", t);
      }, function (e) {
        resume("throw", e);
      });
    } catch (e) {
      settle("throw", e);
    }
  }
  function settle(e, n) {
    switch (e) {
      case "return":
        r.resolve({
          value: n,
          done: !0
        });
        break;
      case "throw":
        r.reject(n);
        break;
      default:
        r.resolve({
          value: n,
          done: !1
        });
    }
    (r = r.next) ? resume(r.key, r.arg) : t = null;
  }
  this._invoke = function (e, n) {
    return new Promise(function (o, u) {
      var i = {
        key: e,
        arg: n,
        resolve: o,
        reject: u,
        next: null
      };
      t ? t = t.next = i : (r = t = i, resume(e, n));
    });
  }, "function" != typeof e.return && (this.return = void 0);
}
AsyncGenerator.prototype["function" == typeof Symbol && Symbol.asyncIterator || "@@asyncIterator"] = function () {
  return this;
}, AsyncGenerator.prototype.next = function (e) {
  return this._invoke("next", e);
}, AsyncGenerator.prototype.throw = function (e) {
  return this._invoke("throw", e);
}, AsyncGenerator.prototype.return = function (e) {
  return this._invoke("return", e);
};

function _asyncIterator(r) {
  var n,
    t,
    o,
    e = 2;
  for ("undefined" != typeof Symbol && (t = Symbol.asyncIterator, o = Symbol.iterator); e--;) {
    if (t && null != (n = r[t])) return n.call(r);
    if (o && null != (n = r[o])) return new AsyncFromSyncIterator(n.call(r));
    t = "@@asyncIterator", o = "@@iterator";
  }
  throw new TypeError("Object is not async iterable");
}
function AsyncFromSyncIterator(r) {
  function AsyncFromSyncIteratorContinuation(r) {
    if (Object(r) !== r) return Promise.reject(new TypeError(r + " is not an object."));
    var n = r.done;
    return Promise.resolve(r.value).then(function (r) {
      return {
        value: r,
        done: n
      };
    });
  }
  return AsyncFromSyncIterator = function (r) {
    this.s = r, this.n = r.next;
  }, AsyncFromSyncIterator.prototype = {
    s: null,
    n: null,
    next: function () {
      return AsyncFromSyncIteratorContinuation(this.n.apply(this.s, arguments));
    },
    return: function (r) {
      var n = this.s.return;
      return void 0 === n ? Promise.resolve({
        value: r,
        done: !0
      }) : AsyncFromSyncIteratorContinuation(n.apply(this.s, arguments));
    },
    throw: function (r) {
      var n = this.s.return;
      return void 0 === n ? Promise.reject(r) : AsyncFromSyncIteratorContinuation(n.apply(this.s, arguments));
    }
  }, new AsyncFromSyncIterator(r);
}

// ANSI color codes
var CYAN = '\x1b[36m';
var GREEN = '\x1b[32m';
var RED = '\x1b[31m';
var RESET = '\x1b[0m';

/**
 * Utility function to wait for user to press Enter
 * Returns a promise that resolves when Enter is pressed, rejects otherwise
 */
function waitForEnterKey() {
  return _waitForEnterKey.apply(this, arguments);
}
/**
 * Interface for a chat message
 */
function _waitForEnterKey() {
  _waitForEnterKey = _asyncToGenerator(/*#__PURE__*/_regeneratorRuntime().mark(function _callee2() {
    return _regeneratorRuntime().wrap(function _callee2$(_context2) {
      while (1) switch (_context2.prev = _context2.next) {
        case 0:
          return _context2.abrupt("return", new Promise(function (resolve, reject) {
            // Set up stdin to read input
            process.stdin.setRawMode(true);
            process.stdin.resume();
            process.stdin.setEncoding('utf8');
            console.log("".concat(GREEN, "Press Enter to continue or any other key to cancel...").concat(RESET));
            var _onData = function onData(key) {
              // Ctrl+C or q to exit
              if (key === "\x03" || key === 'q') {
                process.stdin.setRawMode(false);
                process.stdin.pause();
                process.stdin.removeListener('data', _onData);
                reject(new Error('User cancelled operation'));
                return;
              }

              // Enter key
              if (key === '\r' || key === '\n') {
                process.stdin.setRawMode(false);
                process.stdin.pause();
                process.stdin.removeListener('data', _onData);
                resolve();
              }
            };
            process.stdin.on('data', _onData);
          }));
        case 1:
        case "end":
          return _context2.stop();
      }
    }, _callee2);
  }));
  return _waitForEnterKey.apply(this, arguments);
}
/**
 * Options for the Ollama API call
 */
var OLLAMA_RESPONSE_CACHE_DIR = path.join(process.cwd(), '_ollama_response_cache');

/**
 * Creates a hash of the Ollama request to use as a cache key
 */
function createOllamaRequestHash(messages, options) {
  // Create a string representation of the input that includes all relevant data
  var inputString = JSON.stringify({
    messages: messages,
    options: _objectSpread2(_objectSpread2({}, options), {}, {
      // Exclude stream as it's always false for JSON responses
      stream: undefined
    })
  });

  // Create a SHA-256 hash of the input string
  return crypto.createHash('sha256').update(inputString).digest('hex');
}

/**
 * Ensures the Ollama response cache directory exists
 */
function ensureOllamaCacheDir() {
  fs.mkdirSync(OLLAMA_RESPONSE_CACHE_DIR, {
    recursive: true
  });
  return OLLAMA_RESPONSE_CACHE_DIR;
}

/**
 * Gets the path to the cache file for a specific Ollama request hash
 */
function getOllamaCachePath(requestHash) {
  return path.join(ensureOllamaCacheDir(), "response_".concat(requestHash, ".json"));
}

/**
 * Checks if a cached response exists for the given hash
 */
function hasCachedResponse(requestHash) {
  var cachePath = getOllamaCachePath(requestHash);
  return fs.existsSync(cachePath);
}

/**
 * Reads a cached response from disk
 */
function readCachedResponse(requestHash) {
  var cachePath = getOllamaCachePath(requestHash);
  try {
    var cacheContent = fs.readFileSync(cachePath, 'utf-8');
    return JSON.parse(cacheContent);
  } catch (error) {
    console.error("Error reading Ollama response cache: ".concat(error));
    throw error;
  }
}

/**
 * Writes an Ollama response to the cache
 */
function writeResponseCache(requestHash, response) {
  var cachePath = getOllamaCachePath(requestHash);
  try {
    fs.writeFileSync(cachePath, JSON.stringify(response, null, 2), 'utf-8');
    // console.log(`Ollama response cache written to: ${cachePath}`);
  } catch (error) {
    console.error("Error writing Ollama response cache: ".concat(error));
  }
}

/**
 * Response wrapper type for JSON responses from Ollama
 */

var MAX_JSON_PARSE_ATTEMPTS = 3;

/**
 * Strips any <think>...</think> tags from text
 */
function stripThinkTags(text) {
  return text.replace(/<think>[\s\S]*?<\/think>/g, '').trim();
}

/**
 * Generates a JSON response from Ollama using the provided messages
 * 
 * @param messages - Array of chat messages to send to Ollama
 * @param options - Configuration options for the Ollama API call
 * @param cache - Whether to use caching (defaults to true)
 * @param logStream - Whether to log the streaming response to console (defaults to true)
 * @param schema - Optional Zod schema for response validation and type inference
 * @returns A wrapped response containing either the parsed JSON of type T or a failure status
 */
function generateJSONResponseOllama(_x) {
  return _generateJSONResponseOllama.apply(this, arguments);
}

/**
 * Generates a response from Ollama using the provided messages
 * 
 * @param messages - Array of chat messages to send to Ollama
 * @param options - Configuration options for the Ollama API call
 * @returns The full response text from Ollama
 */
function _generateJSONResponseOllama() {
  _generateJSONResponseOllama = _asyncToGenerator(/*#__PURE__*/_regeneratorRuntime().mark(function _callee4(_ref2) {
    var messages, options, _ref2$cache, cache, _ref2$logStream, logStream, _ref2$printPrompt, printPrompt, _ref2$requireConfirma, requireConfirmation, schema, requestHash, cachedResponse, validatedResponse, formattedPrompts, _loop, _iteratorAbruptCompletion, _didIteratorError, _iteratorError, _iterator, _step, _ret, attempt;
    return _regeneratorRuntime().wrap(function _callee4$(_context5) {
      while (1) switch (_context5.prev = _context5.next) {
        case 0:
          messages = _ref2.messages, options = _ref2.options, _ref2$cache = _ref2.cache, cache = _ref2$cache === void 0 ? true : _ref2$cache, _ref2$logStream = _ref2.logStream, logStream = _ref2$logStream === void 0 ? true : _ref2$logStream, _ref2$printPrompt = _ref2.printPrompt, printPrompt = _ref2$printPrompt === void 0 ? false : _ref2$printPrompt, _ref2$requireConfirma = _ref2.requireConfirmation, requireConfirmation = _ref2$requireConfirma === void 0 ? false : _ref2$requireConfirma, schema = _ref2.schema;
          _context5.prev = 1;
          if (!cache) {
            _context5.next = 18;
            break;
          }
          requestHash = createOllamaRequestHash(messages, options);
          if (!hasCachedResponse(requestHash)) {
            _context5.next = 18;
            break;
          }
          cachedResponse = readCachedResponse(requestHash);
          if (!schema) {
            _context5.next = 17;
            break;
          }
          _context5.prev = 7;
          validatedResponse = schema.parse(cachedResponse);
          return _context5.abrupt("return", {
            status: 'success',
            result: validatedResponse
          });
        case 12:
          _context5.prev = 12;
          _context5.t0 = _context5["catch"](7);
          if (_context5.t0 instanceof z.ZodError) {
            console.warn('Cached response failed schema validation:');
            console.error("".concat(RED).concat(JSON.stringify(_context5.t0.format(), null, 2)).concat(RESET));
          } else {
            console.warn('Cached response failed schema validation:', _context5.t0);
          }
          // Fall through to generate new response
        case 15:
          _context5.next = 18;
          break;
        case 17:
          return _context5.abrupt("return", {
            status: 'success',
            result: cachedResponse
          });
        case 18:
          if (printPrompt || requireConfirmation) {
            // use a different color (green) for the prompt
            formattedPrompts = messages.map(function (message) {
              return message.role + ': ' + message.content.replace(/^```json\s*|\s*```$/g, '');
            }).join('\n\n');
            console.log("".concat(GREEN, "Prompt:").concat(RESET), formattedPrompts);
          }

          // If confirmation is required, wait for user to press Enter
          if (!requireConfirmation) {
            _context5.next = 30;
            break;
          }
          _context5.prev = 20;
          _context5.next = 23;
          return waitForEnterKey();
        case 23:
          console.log("".concat(GREEN, "Proceeding with Ollama request...").concat(RESET));
          _context5.next = 30;
          break;
        case 26:
          _context5.prev = 26;
          _context5.t1 = _context5["catch"](20);
          console.log("".concat(RED, "Operation cancelled by user").concat(RESET));
          throw _context5.t1;
        case 30:
          _loop = /*#__PURE__*/_regeneratorRuntime().mark(function _loop() {
            var _options$temperature, response, fullResponse, streamParts, streamGenerator, part, content, parsedResponse, _validatedResponse, _requestHash, _requestHash2;
            return _regeneratorRuntime().wrap(function _loop$(_context4) {
              while (1) switch (_context4.prev = _context4.next) {
                case 0:
                  _context4.prev = 0;
                  _context4.next = 3;
                  return ollama.chat({
                    model: options.model,
                    format: 'json',
                    options: _objectSpread2({
                      temperature: (_options$temperature = options.temperature) !== null && _options$temperature !== void 0 ? _options$temperature : 0.2,
                      // Lower default temperature for JSON responses
                      top_p: options.top_p,
                      top_k: options.top_k,
                      num_ctx: 1024 * 6
                    }, Object.fromEntries(Object.entries(options).filter(function (_ref4) {
                      var _ref5 = _slicedToArray(_ref4, 1),
                        key = _ref5[0];
                      return !['model', 'stream'].includes(key);
                    }))),
                    messages: messages,
                    stream: true // Enable streaming for console output
                  });
                case 3:
                  response = _context4.sent;
                  fullResponse = '';
                  if (logStream) {
                    process.stdout.write("\nStreaming response".concat(attempt > 1 ? " (attempt ".concat(attempt, "/").concat(MAX_JSON_PARSE_ATTEMPTS, ")") : '', ": "));
                  }

                  // Buffer to store the stream chunks for later replay
                  streamParts = []; // Create the stream generator that will be returned
                  streamGenerator = _wrapAsyncGenerator(/*#__PURE__*/_regeneratorRuntime().mark(function _callee3() {
                    var _i2, _streamParts, part;
                    return _regeneratorRuntime().wrap(function _callee3$(_context3) {
                      while (1) switch (_context3.prev = _context3.next) {
                        case 0:
                          _i2 = 0, _streamParts = streamParts;
                        case 1:
                          if (!(_i2 < _streamParts.length)) {
                            _context3.next = 8;
                            break;
                          }
                          part = _streamParts[_i2];
                          _context3.next = 5;
                          return part;
                        case 5:
                          _i2++;
                          _context3.next = 1;
                          break;
                        case 8:
                        case "end":
                          return _context3.stop();
                      }
                    }, _callee3);
                  }))(); // Collect streamed response while printing to console
                  _iteratorAbruptCompletion = false;
                  _didIteratorError = false;
                  _context4.prev = 10;
                  _iterator = _asyncIterator(response);
                case 12:
                  _context4.next = 14;
                  return _iterator.next();
                case 14:
                  if (!(_iteratorAbruptCompletion = !(_step = _context4.sent).done)) {
                    _context4.next = 23;
                    break;
                  }
                  part = _step.value;
                  content = part.message.content;
                  if (logStream) {
                    process.stdout.write("".concat(CYAN).concat(content).concat(RESET));
                  }
                  // Store the content for later replay through the generator
                  streamParts.push(content);
                  fullResponse += content;
                case 20:
                  _iteratorAbruptCompletion = false;
                  _context4.next = 12;
                  break;
                case 23:
                  _context4.next = 29;
                  break;
                case 25:
                  _context4.prev = 25;
                  _context4.t0 = _context4["catch"](10);
                  _didIteratorError = true;
                  _iteratorError = _context4.t0;
                case 29:
                  _context4.prev = 29;
                  _context4.prev = 30;
                  if (!(_iteratorAbruptCompletion && _iterator["return"] != null)) {
                    _context4.next = 34;
                    break;
                  }
                  _context4.next = 34;
                  return _iterator["return"]();
                case 34:
                  _context4.prev = 34;
                  if (!_didIteratorError) {
                    _context4.next = 37;
                    break;
                  }
                  throw _iteratorError;
                case 37:
                  return _context4.finish(34);
                case 38:
                  return _context4.finish(29);
                case 39:
                  if (logStream) {
                    process.stdout.write('\n'); // Add newline after streaming
                  }

                  // Strip think tags if enabled (defaults to true)
                  if (options.stripThinkTag !== false) {
                    fullResponse = stripThinkTags(fullResponse);
                  }

                  // Parse the complete response as JSON
                  parsedResponse = JSON.parse(fullResponse); // Validate against schema if provided
                  if (!schema) {
                    _context4.next = 48;
                    break;
                  }
                  _validatedResponse = schema.parse(parsedResponse);
                  if (cache) {
                    _requestHash = createOllamaRequestHash(messages, options);
                    writeResponseCache(_requestHash, _validatedResponse);
                  }
                  return _context4.abrupt("return", {
                    v: {
                      status: 'success',
                      result: _validatedResponse,
                      stream: streamGenerator
                    }
                  });
                case 48:
                  if (cache) {
                    _requestHash2 = createOllamaRequestHash(messages, options);
                    writeResponseCache(_requestHash2, parsedResponse);
                  }
                  return _context4.abrupt("return", {
                    v: {
                      status: 'success',
                      result: parsedResponse,
                      stream: streamGenerator
                    }
                  });
                case 50:
                  _context4.next = 60;
                  break;
                case 52:
                  _context4.prev = 52;
                  _context4.t1 = _context4["catch"](0);
                  console.warn("Error parsing JSON response (attempt ".concat(attempt, "/").concat(MAX_JSON_PARSE_ATTEMPTS, ")."));
                  if (!(attempt === MAX_JSON_PARSE_ATTEMPTS)) {
                    _context4.next = 58;
                    break;
                  }
                  console.error('All attempts to parse JSON response failed');
                  return _context4.abrupt("return", {
                    v: {
                      status: 'failed'
                    }
                  });
                case 58:
                  _context4.next = 60;
                  return new Promise(function (resolve) {
                    return setTimeout(resolve, 1000);
                  });
                case 60:
                case "end":
                  return _context4.stop();
              }
            }, _loop, null, [[0, 52], [10, 25, 29, 39], [30,, 34, 38]]);
          });
          attempt = 1;
        case 32:
          if (!(attempt <= MAX_JSON_PARSE_ATTEMPTS)) {
            _context5.next = 40;
            break;
          }
          return _context5.delegateYield(_loop(), "t2", 34);
        case 34:
          _ret = _context5.t2;
          if (!_ret) {
            _context5.next = 37;
            break;
          }
          return _context5.abrupt("return", _ret.v);
        case 37:
          attempt++;
          _context5.next = 32;
          break;
        case 40:
          return _context5.abrupt("return", {
            status: 'failed'
          });
        case 43:
          _context5.prev = 43;
          _context5.t3 = _context5["catch"](1);
          console.error('Error generating JSON response from Ollama:', _context5.t3);
          console.error("Messages:", messages);
          return _context5.abrupt("return", {
            status: 'failed'
          });
        case 48:
        case "end":
          return _context5.stop();
      }
    }, _callee4, null, [[1, 43], [7, 12], [20, 26]]);
  }));
  return _generateJSONResponseOllama.apply(this, arguments);
}
function generateResponseOllama(_x2) {
  return _generateResponseOllama.apply(this, arguments);
}

/**
 * Extracts JSON content from a response string
 * 
 * @param response - The full response text from Ollama
 * @returns The extracted JSON content, or null if no valid JSON was found
 */
function _generateResponseOllama() {
  _generateResponseOllama = _asyncToGenerator(/*#__PURE__*/_regeneratorRuntime().mark(function _callee5(_ref3) {
    var messages, options, jsonResponse, cacheEnabled, requestHash, cachedResponse, streamEnabled, _options$temperature2, response, fullResponse, _iteratorAbruptCompletion2, _didIteratorError2, _iteratorError2, _iterator2, _step2, part, _requestHash3, _options$temperature3, _response, content, _requestHash4;
    return _regeneratorRuntime().wrap(function _callee5$(_context6) {
      while (1) switch (_context6.prev = _context6.next) {
        case 0:
          messages = _ref3.messages, options = _ref3.options;
          _context6.prev = 1;
          if (!options.json) {
            _context6.next = 9;
            break;
          }
          _context6.next = 5;
          return generateJSONResponseOllama({
            messages: messages,
            options: options
          });
        case 5:
          jsonResponse = _context6.sent;
          if (!(jsonResponse.status === 'failed')) {
            _context6.next = 8;
            break;
          }
          return _context6.abrupt("return", '{}');
        case 8:
          return _context6.abrupt("return", JSON.stringify(jsonResponse.result, null, 2));
        case 9:
          // Check cache if enabled (defaults to true)
          cacheEnabled = options.cache !== false;
          if (!cacheEnabled) {
            _context6.next = 15;
            break;
          }
          requestHash = createOllamaRequestHash(messages, options);
          if (!hasCachedResponse(requestHash)) {
            _context6.next = 15;
            break;
          }
          cachedResponse = readCachedResponse(requestHash);
          return _context6.abrupt("return", cachedResponse);
        case 15:
          // Set default stream to true if not specified
          streamEnabled = options.stream !== false;
          if (!streamEnabled) {
            _context6.next = 55;
            break;
          }
          _context6.next = 19;
          return ollama.chat({
            model: options.model,
            options: _objectSpread2({
              temperature: (_options$temperature2 = options.temperature) !== null && _options$temperature2 !== void 0 ? _options$temperature2 : 0.0,
              top_p: options.top_p,
              top_k: options.top_k,
              num_ctx: 1024 * 6
            }, Object.fromEntries(Object.entries(options).filter(function (_ref6) {
              var _ref7 = _slicedToArray(_ref6, 1),
                key = _ref7[0];
              return !['model', 'stream'].includes(key);
            }))),
            messages: messages,
            stream: true
          });
        case 19:
          response = _context6.sent;
          fullResponse = '';
          _iteratorAbruptCompletion2 = false;
          _didIteratorError2 = false;
          _context6.prev = 23;
          _iterator2 = _asyncIterator(response);
        case 25:
          _context6.next = 27;
          return _iterator2.next();
        case 27:
          if (!(_iteratorAbruptCompletion2 = !(_step2 = _context6.sent).done)) {
            _context6.next = 34;
            break;
          }
          part = _step2.value;
          process.stdout.write("".concat(CYAN).concat(part.message.content).concat(RESET));
          fullResponse += part.message.content;
        case 31:
          _iteratorAbruptCompletion2 = false;
          _context6.next = 25;
          break;
        case 34:
          _context6.next = 40;
          break;
        case 36:
          _context6.prev = 36;
          _context6.t0 = _context6["catch"](23);
          _didIteratorError2 = true;
          _iteratorError2 = _context6.t0;
        case 40:
          _context6.prev = 40;
          _context6.prev = 41;
          if (!(_iteratorAbruptCompletion2 && _iterator2["return"] != null)) {
            _context6.next = 45;
            break;
          }
          _context6.next = 45;
          return _iterator2["return"]();
        case 45:
          _context6.prev = 45;
          if (!_didIteratorError2) {
            _context6.next = 48;
            break;
          }
          throw _iteratorError2;
        case 48:
          return _context6.finish(45);
        case 49:
          return _context6.finish(40);
        case 50:
          // Strip think tags if enabled (defaults to true)
          if (options.stripThinkTag !== false) {
            fullResponse = stripThinkTags(fullResponse);
          }

          // Cache the response if enabled
          if (cacheEnabled) {
            _requestHash3 = createOllamaRequestHash(messages, options);
            writeResponseCache(_requestHash3, fullResponse);
          }
          return _context6.abrupt("return", fullResponse);
        case 55:
          _context6.next = 57;
          return ollama.chat({
            model: options.model,
            options: _objectSpread2({
              temperature: (_options$temperature3 = options.temperature) !== null && _options$temperature3 !== void 0 ? _options$temperature3 : 0.7,
              top_p: options.top_p,
              top_k: options.top_k
            }, Object.fromEntries(Object.entries(options).filter(function (_ref8) {
              var _ref9 = _slicedToArray(_ref8, 1),
                key = _ref9[0];
              return !['model', 'stream'].includes(key);
            }))),
            messages: messages,
            stream: false
          });
        case 57:
          _response = _context6.sent;
          content = _response.message.content; // Strip think tags if enabled (defaults to true)
          if (options.stripThinkTag !== false) {
            content = stripThinkTags(content);
          }

          // Cache the response if enabled
          if (cacheEnabled) {
            _requestHash4 = createOllamaRequestHash(messages, options);
            writeResponseCache(_requestHash4, content);
          }
          return _context6.abrupt("return", content);
        case 62:
          _context6.next = 69;
          break;
        case 64:
          _context6.prev = 64;
          _context6.t1 = _context6["catch"](1);
          console.error('Error generating response from Ollama:', _context6.t1);
          console.error("Messages:", messages);
          throw _context6.t1;
        case 69:
        case "end":
          return _context6.stop();
      }
    }, _callee5, null, [[1, 64], [23, 36, 40, 50], [41,, 45, 49]]);
  }));
  return _generateResponseOllama.apply(this, arguments);
}
function extractJsonFromResponse(response) {
  var _response$matchAll, _allJsonMatches, _lastJsonMatch$1$trim, _lastJsonMatch$;
  var allJsonMatches = Array.from((_response$matchAll = response.matchAll(/```json([\s\S]*?)```/g)) !== null && _response$matchAll !== void 0 ? _response$matchAll : []);
  var lastJsonMatch = (_allJsonMatches = allJsonMatches[allJsonMatches.length - 1]) !== null && _allJsonMatches !== void 0 ? _allJsonMatches : null;
  return (_lastJsonMatch$1$trim = lastJsonMatch === null || lastJsonMatch === void 0 || (_lastJsonMatch$ = lastJsonMatch[1]) === null || _lastJsonMatch$ === void 0 ? void 0 : _lastJsonMatch$.trim()) !== null && _lastJsonMatch$1$trim !== void 0 ? _lastJsonMatch$1$trim : null;
}
function extractLineByLineFromResponse(response) {
  var _response$matchAll2, _allLineByLineMatches, _lastLineByLineMatch$, _lastLineByLineMatch$2;
  var allLineByLineMatches = Array.from((_response$matchAll2 = response.matchAll(/```line_by_line([\s\S]*?)```/g)) !== null && _response$matchAll2 !== void 0 ? _response$matchAll2 : []);
  var lastLineByLineMatch = (_allLineByLineMatches = allLineByLineMatches[allLineByLineMatches.length - 1]) !== null && _allLineByLineMatches !== void 0 ? _allLineByLineMatches : null;
  return (_lastLineByLineMatch$ = lastLineByLineMatch === null || lastLineByLineMatch === void 0 || (_lastLineByLineMatch$2 = lastLineByLineMatch[1]) === null || _lastLineByLineMatch$2 === void 0 ? void 0 : _lastLineByLineMatch$2.trim()) !== null && _lastLineByLineMatch$ !== void 0 ? _lastLineByLineMatch$ : null;
}

/**
 * Calculates the Levenshtein distance between two strings
 * 
 * @param str1 - First string to compare
 * @param str2 - Second string to compare
 * @returns The Levenshtein distance (number of edits needed to transform str1 into str2)
 */
function levenshteinDistance(str1, str2) {
  var m = str1.length;
  var n = str2.length;
  var dp = Array(m + 1).fill(null).map(function () {
    return Array(n + 1).fill(0);
  });
  for (var i = 0; i <= m; i++) dp[i][0] = i;
  for (var j = 0; j <= n; j++) dp[0][j] = j;
  for (var _i = 1; _i <= m; _i++) {
    for (var _j = 1; _j <= n; _j++) {
      if (str1[_i - 1] === str2[_j - 1]) {
        dp[_i][_j] = dp[_i - 1][_j - 1];
      } else {
        dp[_i][_j] = Math.min(dp[_i - 1][_j - 1] + 1,
        // substitution
        dp[_i - 1][_j] + 1,
        // deletion
        dp[_i][_j - 1] + 1 // insertion
        );
      }
    }
  }
  return dp[m][n];
}

/**
 * Calculates the similarity ratio between two strings (0 to 1)
 * 
 * @param str1 - First string to compare
 * @param str2 - Second string to compare
 * @returns A value between 0 (completely different) and 1 (identical)
 */
function stringSimilarity(str1, str2) {
  var maxLength = Math.max(str1.length, str2.length);
  if (maxLength === 0) return 1.0; // Both strings are empty
  var distance = levenshteinDistance(str1, str2);
  return 1 - distance / maxLength;
}
var EMBEDDING_MODEL = 'nomic-embed-text'; // Ollama embedding model

/**
 * Generate embedding for a text description using Ollama
 * @param text Text to generate embeddings for
 * @returns Array of embedding values
 */
function genEmbeddingOllama(_x3) {
  return _genEmbeddingOllama.apply(this, arguments);
}
function _genEmbeddingOllama() {
  _genEmbeddingOllama = _asyncToGenerator(/*#__PURE__*/_regeneratorRuntime().mark(function _callee6(text) {
    var response;
    return _regeneratorRuntime().wrap(function _callee6$(_context7) {
      while (1) switch (_context7.prev = _context7.next) {
        case 0:
          _context7.prev = 0;
          _context7.next = 3;
          return ollama.embeddings({
            model: EMBEDDING_MODEL,
            prompt: text
          });
        case 3:
          response = _context7.sent;
          return _context7.abrupt("return", response.embedding);
        case 7:
          _context7.prev = 7;
          _context7.t0 = _context7["catch"](0);
          console.error('Error generating embedding:', _context7.t0);
          throw _context7.t0;
        case 11:
        case "end":
          return _context7.stop();
      }
    }, _callee6, null, [[0, 7]]);
  }));
  return _genEmbeddingOllama.apply(this, arguments);
}
var EmbeddingCache = /*#__PURE__*/function () {
  function EmbeddingCache() {
    var cacheDir = arguments.length > 0 && arguments[0] !== undefined ? arguments[0] : '.cache/embeddings';
    _classCallCheck(this, EmbeddingCache);
    this.cacheDir = cacheDir;
    this.cache = new Map();
    this.initializeCache();
  }
  return _createClass(EmbeddingCache, [{
    key: "initializeCache",
    value: function initializeCache() {
      // Create cache directory if it doesn't exist
      if (!fs.existsSync(this.cacheDir)) {
        fs.mkdirSync(this.cacheDir, {
          recursive: true
        });
      }
    }
  }, {
    key: "getHash",
    value: function getHash(text) {
      return crypto.createHash('sha256').update(text).digest('hex');
    }
  }, {
    key: "getCacheFilePath",
    value: function getCacheFilePath(hash) {
      return path.join(this.cacheDir, "".concat(hash, ".bin"));
    }
  }, {
    key: "arrayToBuffer",
    value: function arrayToBuffer(arr) {
      var buffer = Buffer.alloc(arr.length * Float32Array.BYTES_PER_ELEMENT);
      var view = new Float32Array(buffer.buffer);
      arr.forEach(function (val, i) {
        return view[i] = val;
      });
      return buffer;
    }
  }, {
    key: "bufferToArray",
    value: function bufferToArray(buffer) {
      var view = new Float32Array(buffer.buffer, buffer.byteOffset, buffer.length / Float32Array.BYTES_PER_ELEMENT);
      return Array.from(view);
    }
  }, {
    key: "getEmbedding",
    value: function () {
      var _getEmbedding = _asyncToGenerator(/*#__PURE__*/_regeneratorRuntime().mark(function _callee(text) {
        var hash, cacheFilePath, _buffer, _embedding, embedding, buffer;
        return _regeneratorRuntime().wrap(function _callee$(_context) {
          while (1) switch (_context.prev = _context.next) {
            case 0:
              hash = this.getHash(text); // Check in-memory cache first
              if (!this.cache.has(hash)) {
                _context.next = 3;
                break;
              }
              return _context.abrupt("return", this.cache.get(hash));
            case 3:
              // Check file cache
              cacheFilePath = this.getCacheFilePath(hash);
              if (!fs.existsSync(cacheFilePath)) {
                _context.next = 9;
                break;
              }
              _buffer = fs.readFileSync(cacheFilePath);
              _embedding = this.bufferToArray(_buffer);
              this.cache.set(hash, _embedding);
              return _context.abrupt("return", _embedding);
            case 9:
              _context.next = 11;
              return genEmbeddingOllama(text);
            case 11:
              embedding = _context.sent;
              // Save to both caches
              this.cache.set(hash, embedding);
              buffer = this.arrayToBuffer(embedding);
              fs.writeFileSync(cacheFilePath, buffer);
              return _context.abrupt("return", embedding);
            case 16:
            case "end":
              return _context.stop();
          }
        }, _callee, this);
      }));
      function getEmbedding(_x4) {
        return _getEmbedding.apply(this, arguments);
      }
      return getEmbedding;
    }()
    /**
     * Calculate cosine similarity between two embeddings
     */
  }, {
    key: "getCosineSimilarity",
    value: function getCosineSimilarity(embedding1, embedding2) {
      if (!Array.isArray(embedding1) || !Array.isArray(embedding2) || embedding1.length !== embedding2.length || embedding1.length === 0) {
        console.warn('Invalid embeddings provided for cosine similarity calculation');
        return 0;
      }
      var dotProduct = 0;
      var normA = 0;
      var normB = 0;
      for (var i = 0; i < embedding1.length; i++) {
        if (!Number.isFinite(embedding1[i]) || !Number.isFinite(embedding2[i])) continue;
        dotProduct += embedding1[i] * embedding2[i];
        normA += embedding1[i] * embedding1[i];
        normB += embedding2[i] * embedding2[i];
      }
      if (normA === 0 || normB === 0) {
        console.warn('Zero norm detected in cosine similarity calculation');
        return 0;
      }
      var similarity = dotProduct / (Math.sqrt(normA) * Math.sqrt(normB));
      return Math.max(-1, Math.min(1, similarity));
    }
  }]);
}();
var cache = new EmbeddingCache();

// cached version of the embedding function
function generateEmbedding(_x5) {
  return _generateEmbedding.apply(this, arguments);
}
function _generateEmbedding() {
  _generateEmbedding = _asyncToGenerator(/*#__PURE__*/_regeneratorRuntime().mark(function _callee7(text) {
    return _regeneratorRuntime().wrap(function _callee7$(_context8) {
      while (1) switch (_context8.prev = _context8.next) {
        case 0:
          return _context8.abrupt("return", cache.getEmbedding(text));
        case 1:
        case "end":
          return _context8.stop();
      }
    }, _callee7);
  }));
  return _generateEmbedding.apply(this, arguments);
}
function getCosineSimilarity(_x6, _x7) {
  return _getCosineSimilarity.apply(this, arguments);
}
function _getCosineSimilarity() {
  _getCosineSimilarity = _asyncToGenerator(/*#__PURE__*/_regeneratorRuntime().mark(function _callee8(embedding1, embedding2) {
    return _regeneratorRuntime().wrap(function _callee8$(_context9) {
      while (1) switch (_context9.prev = _context9.next) {
        case 0:
          return _context9.abrupt("return", cache.getCosineSimilarity(embedding1, embedding2));
        case 1:
        case "end":
          return _context9.stop();
      }
    }, _callee8);
  }));
  return _getCosineSimilarity.apply(this, arguments);
}

export { EmbeddingCache, extractJsonFromResponse, extractLineByLineFromResponse, generateEmbedding, generateJSONResponseOllama, generateResponseOllama, getCosineSimilarity, levenshteinDistance, stringSimilarity };
