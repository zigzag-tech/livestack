import { createHmac, randomUUID } from 'node:crypto';

function _typeof(o) {
  "@babel/helpers - typeof";

  return _typeof = "function" == typeof Symbol && "symbol" == typeof Symbol.iterator ? function (o) {
    return typeof o;
  } : function (o) {
    return o && "function" == typeof Symbol && o.constructor === Symbol && o !== Symbol.prototype ? "symbol" : typeof o;
  }, _typeof(o);
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

function _arrayLikeToArray(r, a) {
  (null == a || a > r.length) && (a = r.length);
  for (var e = 0, n = Array(a); e < a; e++) n[e] = r[e];
  return n;
}

function _arrayWithoutHoles(r) {
  if (Array.isArray(r)) return _arrayLikeToArray(r);
}

function _iterableToArray(r) {
  if ("undefined" != typeof Symbol && null != r[Symbol.iterator] || null != r["@@iterator"]) return Array.from(r);
}

function _unsupportedIterableToArray(r, a) {
  if (r) {
    if ("string" == typeof r) return _arrayLikeToArray(r, a);
    var t = {}.toString.call(r).slice(8, -1);
    return "Object" === t && r.constructor && (t = r.constructor.name), "Map" === t || "Set" === t ? Array.from(r) : "Arguments" === t || /^(?:Ui|I)nt(?:8|16|32)(?:Clamped)?Array$/.test(t) ? _arrayLikeToArray(r, a) : void 0;
  }
}

function _nonIterableSpread() {
  throw new TypeError("Invalid attempt to spread non-iterable instance.\nIn order to be iterable, non-array objects must have a [Symbol.iterator]() method.");
}

function _toConsumableArray(r) {
  return _arrayWithoutHoles(r) || _iterableToArray(r) || _unsupportedIterableToArray(r) || _nonIterableSpread();
}

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

function _nonIterableRest() {
  throw new TypeError("Invalid attempt to destructure non-iterable instance.\nIn order to be iterable, non-array objects must have a [Symbol.iterator]() method.");
}

function _slicedToArray(r, e) {
  return _arrayWithHoles(r) || _iterableToArrayLimit(r, e) || _unsupportedIterableToArray(r, e) || _nonIterableRest();
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

var HEYUAN_G8I_2XLARGE_RENDER_PROFILE = {
  regionId: "cn-heyuan",
  instanceType: "ecs.g8i.2xlarge",
  imageFamily: "ubuntu_24_04_x64",
  systemDiskCategory: "cloud_essd",
  systemDiskSizeGiB: 80,
  autoReleaseHours: 4,
  idleShutdownSeconds: 300,
  maxParallelWorkers: 4,
  labels: {
    workload: "remotion-render",
    sizingPolicy: "scale-out-g8i-2xlarge"
  }
};
function aliyunWorkerHostId(profile, instanceId) {
  return "aliyun-".concat(profile.regionId, "/").concat(profile.instanceType, "/").concat(instanceId);
}
function createAliyunEcsWorkers(_x, _x2) {
  return _createAliyunEcsWorkers.apply(this, arguments);
}
function _createAliyunEcsWorkers() {
  _createAliyunEcsWorkers = _asyncToGenerator(/*#__PURE__*/_regeneratorRuntime().mark(function _callee5(client, input) {
    var _input$profile2, _input$namePrefix;
    var profile, amount, result;
    return _regeneratorRuntime().wrap(function (_context5) {
      while (1) switch (_context5.prev = _context5.next) {
        case 0:
          profile = (_input$profile2 = input.profile) !== null && _input$profile2 !== void 0 ? _input$profile2 : HEYUAN_G8I_2XLARGE_RENDER_PROFILE;
          amount = Math.max(1, Math.min(input.amount, profile.maxParallelWorkers));
          _context5.next = 1;
          return client.runInstances(buildAliyunRunInstancesRequest({
            profile: profile,
            amount: amount,
            namePrefix: (_input$namePrefix = input.namePrefix) !== null && _input$namePrefix !== void 0 ? _input$namePrefix : "livestack-worker",
            userData: input.userData,
            now: input.now,
            tags: input.tags
          }));
        case 1:
          result = _context5.sent;
          return _context5.abrupt("return", result.InstanceIdSets.InstanceIdSet.map(function (instanceId) {
            return {
              instanceId: instanceId,
              hostId: aliyunWorkerHostId(profile, instanceId),
              regionId: profile.regionId,
              instanceType: profile.instanceType
            };
          }));
        case 2:
        case "end":
          return _context5.stop();
      }
    }, _callee5);
  }));
  return _createAliyunEcsWorkers.apply(this, arguments);
}
function listAliyunEcsWorkers(_x3) {
  return _listAliyunEcsWorkers.apply(this, arguments);
}
function _listAliyunEcsWorkers() {
  _listAliyunEcsWorkers = _asyncToGenerator(/*#__PURE__*/_regeneratorRuntime().mark(function _callee6(client) {
    var _input$profile3, _input$regionId;
    var input,
      profile,
      result,
      _args6 = arguments;
    return _regeneratorRuntime().wrap(function (_context6) {
      while (1) switch (_context6.prev = _context6.next) {
        case 0:
          input = _args6.length > 1 && _args6[1] !== undefined ? _args6[1] : {};
          profile = (_input$profile3 = input.profile) !== null && _input$profile3 !== void 0 ? _input$profile3 : HEYUAN_G8I_2XLARGE_RENDER_PROFILE;
          _context6.next = 1;
          return client.describeInstances(_objectSpread2(_objectSpread2({
            RegionId: (_input$regionId = input.regionId) !== null && _input$regionId !== void 0 ? _input$regionId : profile.regionId
          }, input.instanceIds && input.instanceIds.length > 0 ? {
            InstanceIds: input.instanceIds
          } : {}), {}, {
            Tags: Object.entries(_objectSpread2({
              app: "livestack"
            }, input.tags)).map(function (_ref5) {
              var _ref6 = _slicedToArray(_ref5, 2),
                Key = _ref6[0],
                Value = _ref6[1];
              return {
                Key: Key,
                Value: Value
              };
            }),
            PageSize: 100
          }));
        case 1:
          result = _context6.sent;
          return _context6.abrupt("return", result.Instances.Instance.map(function (instance) {
            var _ref7, _instance$RegionId, _instance$InstanceTyp;
            var regionId = (_ref7 = (_instance$RegionId = instance.RegionId) !== null && _instance$RegionId !== void 0 ? _instance$RegionId : input.regionId) !== null && _ref7 !== void 0 ? _ref7 : profile.regionId;
            var instanceType = (_instance$InstanceTyp = instance.InstanceType) !== null && _instance$InstanceTyp !== void 0 ? _instance$InstanceTyp : profile.instanceType;
            var hostProfile = _objectSpread2(_objectSpread2({}, profile), {}, {
              regionId: regionId,
              instanceType: instanceType
            });
            return _objectSpread2(_objectSpread2(_objectSpread2(_objectSpread2(_objectSpread2({
              instanceId: instance.InstanceId,
              hostId: aliyunWorkerHostId(hostProfile, instance.InstanceId),
              regionId: regionId,
              instanceType: instanceType
            }, instance.Status ? {
              status: instance.Status
            } : {}), instance.InstanceName ? {
              name: instance.InstanceName
            } : {}), instance.CreationTime ? {
              createdAt: instance.CreationTime
            } : {}), instance.ExpiredTime ? {
              expiresAt: instance.ExpiredTime
            } : {}), {}, {
              tags: normalizeTags(instance.Tags)
            });
          }));
        case 2:
        case "end":
          return _context6.stop();
      }
    }, _callee6);
  }));
  return _listAliyunEcsWorkers.apply(this, arguments);
}
function deleteAliyunEcsWorkers(_x4, _x5) {
  return _deleteAliyunEcsWorkers.apply(this, arguments);
}
function _deleteAliyunEcsWorkers() {
  _deleteAliyunEcsWorkers = _asyncToGenerator(/*#__PURE__*/_regeneratorRuntime().mark(function _callee7(client, input) {
    var _input$profile4, _input$regionId2, _input$force;
    var profile;
    return _regeneratorRuntime().wrap(function (_context7) {
      while (1) switch (_context7.prev = _context7.next) {
        case 0:
          if (!(input.instanceIds.length === 0)) {
            _context7.next = 1;
            break;
          }
          return _context7.abrupt("return");
        case 1:
          profile = (_input$profile4 = input.profile) !== null && _input$profile4 !== void 0 ? _input$profile4 : HEYUAN_G8I_2XLARGE_RENDER_PROFILE;
          _context7.next = 2;
          return client.deleteInstances({
            RegionId: (_input$regionId2 = input.regionId) !== null && _input$regionId2 !== void 0 ? _input$regionId2 : profile.regionId,
            InstanceIds: input.instanceIds,
            Force: (_input$force = input.force) !== null && _input$force !== void 0 ? _input$force : true
          });
        case 2:
        case "end":
          return _context7.stop();
      }
    }, _callee7);
  }));
  return _deleteAliyunEcsWorkers.apply(this, arguments);
}
function buildAliyunRunInstancesRequest(input) {
  var _input$now;
  var autoReleaseTime = new Date(((_input$now = input.now) !== null && _input$now !== void 0 ? _input$now : new Date()).getTime() + input.profile.autoReleaseHours * 60 * 60 * 1000).toISOString();
  return _objectSpread2(_objectSpread2({
    RegionId: input.profile.regionId,
    InstanceType: input.profile.instanceType,
    ImageFamily: input.profile.imageFamily,
    Amount: input.amount,
    InstanceName: input.namePrefix,
    HostName: input.namePrefix,
    AutoReleaseTime: autoReleaseTime,
    SystemDisk: {
      Category: input.profile.systemDiskCategory,
      Size: input.profile.systemDiskSizeGiB
    }
  }, input.userData ? {
    UserData: Buffer.from(input.userData, "utf8").toString("base64")
  } : {}), {}, {
    Tags: Object.entries(_objectSpread2(_objectSpread2({
      app: "livestack",
      region: input.profile.regionId,
      instanceType: input.profile.instanceType
    }, input.profile.labels), input.tags)).map(function (_ref) {
      var _ref2 = _slicedToArray(_ref, 2),
        Key = _ref2[0],
        Value = _ref2[1];
      return {
        Key: Key,
        Value: Value
      };
    })
  });
}
function buildAliyunWorkerBootstrapUserData(input) {
  var _input$profile, _input$env, _input$branch, _input$packageManager, _input$workerCommand;
  var profile = (_input$profile = input.profile) !== null && _input$profile !== void 0 ? _input$profile : HEYUAN_G8I_2XLARGE_RENDER_PROFILE;
  var hostPrefix = "aliyun-".concat(profile.regionId, "/").concat(profile.instanceType, "/");
  return ["#!/usr/bin/env bash", "set -euo pipefail", "mkdir -p /root/.livestack", "ALIYUN_INSTANCE_ID=\"$(curl -fsS --connect-timeout 2 http://100.100.100.200/latest/meta-data/instance-id || hostname)\"", "LIVESTACK_HOST_ID=".concat(shellQuote(hostPrefix), "\"$ALIYUN_INSTANCE_ID\""), "printf '%s\\n' \"$LIVESTACK_HOST_ID\" > /root/.livestack/host-id", "export LIVESTACK_HOST_ID"].concat(_toConsumableArray(Object.entries((_input$env = input.env) !== null && _input$env !== void 0 ? _input$env : {}).map(function (_ref3) {
    var _ref4 = _slicedToArray(_ref3, 2),
      key = _ref4[0],
      value = _ref4[1];
    return "export ".concat(key, "=").concat(shellQuote(value));
  })), ["mkdir -p /opt/livestack-worker", "cd /opt/livestack-worker", "git clone --depth 1 --branch ".concat(shellQuote((_input$branch = input.branch) !== null && _input$branch !== void 0 ? _input$branch : "main"), " ").concat(shellQuote(input.repoUrl), " app"), "cd app", (_input$packageManager = input.packageManagerCommand) !== null && _input$packageManager !== void 0 ? _input$packageManager : "npm install", "set +e", (_input$workerCommand = input.workerCommand) !== null && _input$workerCommand !== void 0 ? _input$workerCommand : "npm run worker", "worker_status=$?", "shutdown -h now || true", "exit \"$worker_status\""]).join("\n");
}
function createAliyunEcsClientFromEnv() {
  var _env$ALIBABA_CLOUD_AC, _env$ALIBABA_CLOUD_AC2;
  var env = arguments.length > 0 && arguments[0] !== undefined ? arguments[0] : process.env;
  var accessKeyId = (_env$ALIBABA_CLOUD_AC = env.ALIBABA_CLOUD_ACCESS_KEY_ID) !== null && _env$ALIBABA_CLOUD_AC !== void 0 ? _env$ALIBABA_CLOUD_AC : env.ALIYUN_ACCESS_KEY_ID;
  var accessKeySecret = (_env$ALIBABA_CLOUD_AC2 = env.ALIBABA_CLOUD_ACCESS_KEY_SECRET) !== null && _env$ALIBABA_CLOUD_AC2 !== void 0 ? _env$ALIBABA_CLOUD_AC2 : env.ALIYUN_ACCESS_KEY_SECRET;
  if (!accessKeyId || !accessKeySecret) {
    throw new Error("Missing Aliyun credentials.");
  }
  return new AliyunEcsRpcClient({
    accessKeyId: accessKeyId,
    accessKeySecret: accessKeySecret,
    endpoint: env.ALIYUN_ECS_ENDPOINT
  });
}
var AliyunEcsRpcClient = /*#__PURE__*/function () {
  function AliyunEcsRpcClient(options) {
    var _options$endpoint, _options$fetch, _options$now, _options$nonce;
    _classCallCheck(this, AliyunEcsRpcClient);
    this.options = options;
    this.endpoint = (_options$endpoint = options.endpoint) !== null && _options$endpoint !== void 0 ? _options$endpoint : "https://ecs.aliyuncs.com/";
    this.fetchImpl = (_options$fetch = options.fetch) !== null && _options$fetch !== void 0 ? _options$fetch : fetch;
    this.now = (_options$now = options.now) !== null && _options$now !== void 0 ? _options$now : function () {
      return new Date();
    };
    this.nonce = (_options$nonce = options.nonce) !== null && _options$nonce !== void 0 ? _options$nonce : function () {
      return randomUUID();
    };
  }
  return _createClass(AliyunEcsRpcClient, [{
    key: "runInstances",
    value: function () {
      var _runInstances = _asyncToGenerator(/*#__PURE__*/_regeneratorRuntime().mark(function _callee(request) {
        return _regeneratorRuntime().wrap(function (_context) {
          while (1) switch (_context.prev = _context.next) {
            case 0:
              return _context.abrupt("return", this.call("RunInstances", flattenRunInstancesRequest(request)));
            case 1:
            case "end":
              return _context.stop();
          }
        }, _callee, this);
      }));
      function runInstances(_x6) {
        return _runInstances.apply(this, arguments);
      }
      return runInstances;
    }()
  }, {
    key: "describeInstances",
    value: function () {
      var _describeInstances = _asyncToGenerator(/*#__PURE__*/_regeneratorRuntime().mark(function _callee2(request) {
        return _regeneratorRuntime().wrap(function (_context2) {
          while (1) switch (_context2.prev = _context2.next) {
            case 0:
              return _context2.abrupt("return", this.call("DescribeInstances", flattenDescribeInstancesRequest(request)));
            case 1:
            case "end":
              return _context2.stop();
          }
        }, _callee2, this);
      }));
      function describeInstances(_x7) {
        return _describeInstances.apply(this, arguments);
      }
      return describeInstances;
    }()
  }, {
    key: "deleteInstances",
    value: function () {
      var _deleteInstances = _asyncToGenerator(/*#__PURE__*/_regeneratorRuntime().mark(function _callee3(request) {
        return _regeneratorRuntime().wrap(function (_context3) {
          while (1) switch (_context3.prev = _context3.next) {
            case 0:
              _context3.next = 1;
              return this.call("DeleteInstances", flattenDeleteInstancesRequest(request));
            case 1:
            case "end":
              return _context3.stop();
          }
        }, _callee3, this);
      }));
      function deleteInstances(_x8) {
        return _deleteInstances.apply(this, arguments);
      }
      return deleteInstances;
    }()
  }, {
    key: "call",
    value: function () {
      var _call = _asyncToGenerator(/*#__PURE__*/_regeneratorRuntime().mark(function _callee4(action, params) {
        var signedParams, response, body, parsed, message;
        return _regeneratorRuntime().wrap(function (_context4) {
          while (1) switch (_context4.prev = _context4.next) {
            case 0:
              signedParams = signAliyunRpcRequest({
                accessKeyId: this.options.accessKeyId,
                accessKeySecret: this.options.accessKeySecret,
                method: "POST",
                params: _objectSpread2(_objectSpread2({}, params), {}, {
                  Action: action,
                  Version: "2014-05-26",
                  Format: "JSON",
                  SignatureMethod: "HMAC-SHA1",
                  SignatureNonce: this.nonce(),
                  SignatureVersion: "1.0",
                  Timestamp: this.now().toISOString()
                })
              });
              _context4.next = 1;
              return this.fetchImpl(this.endpoint, {
                method: "POST",
                headers: {
                  "content-type": "application/x-www-form-urlencoded"
                },
                body: new URLSearchParams(signedParams).toString()
              });
            case 1:
              response = _context4.sent;
              _context4.next = 2;
              return response.text();
            case 2:
              body = _context4.sent;
              parsed = body.trim() ? JSON.parse(body) : {};
              if (response.ok) {
                _context4.next = 3;
                break;
              }
              message = _typeof(parsed) === "object" && parsed && "Message" in parsed ? String(parsed.Message) : body.slice(0, 200);
              throw new Error("Aliyun ECS ".concat(action, " failed (").concat(response.status, "): ").concat(message));
            case 3:
              return _context4.abrupt("return", parsed);
            case 4:
            case "end":
              return _context4.stop();
          }
        }, _callee4, this);
      }));
      function call(_x9, _x0) {
        return _call.apply(this, arguments);
      }
      return call;
    }()
  }]);
}();
function signAliyunRpcRequest(input) {
  var params = _objectSpread2(_objectSpread2({}, input.params), {}, {
    AccessKeyId: input.accessKeyId
  });
  var canonical = canonicalizeAliyunParams(params);
  var stringToSign = [input.method, percentEncode("/"), percentEncode(canonical)].join("&");
  var signature = createHmac("sha1", "".concat(input.accessKeySecret, "&")).update(stringToSign).digest("base64");
  return _objectSpread2(_objectSpread2({}, params), {}, {
    Signature: signature
  });
}
function flattenRunInstancesRequest(request) {
  return _objectSpread2(_objectSpread2({
    RegionId: request.RegionId,
    InstanceType: request.InstanceType,
    ImageFamily: request.ImageFamily,
    Amount: String(request.Amount),
    InstanceName: request.InstanceName,
    HostName: request.HostName,
    AutoReleaseTime: request.AutoReleaseTime,
    "SystemDisk.Category": request.SystemDisk.Category,
    "SystemDisk.Size": String(request.SystemDisk.Size)
  }, request.UserData ? {
    UserData: request.UserData
  } : {}), flattenTags(request.Tags));
}
function flattenDescribeInstancesRequest(request) {
  var _request$Tags;
  return _objectSpread2(_objectSpread2(_objectSpread2(_objectSpread2({
    RegionId: request.RegionId
  }, request.InstanceIds && request.InstanceIds.length > 0 ? {
    InstanceIds: JSON.stringify(request.InstanceIds)
  } : {}), request.PageNumber ? {
    PageNumber: String(request.PageNumber)
  } : {}), request.PageSize ? {
    PageSize: String(request.PageSize)
  } : {}), flattenTags((_request$Tags = request.Tags) !== null && _request$Tags !== void 0 ? _request$Tags : []));
}
function flattenDeleteInstancesRequest(input) {
  var _input$Force;
  return _objectSpread2({
    RegionId: input.RegionId,
    Force: String((_input$Force = input.Force) !== null && _input$Force !== void 0 ? _input$Force : true)
  }, Object.fromEntries(input.InstanceIds.map(function (id, index) {
    return ["InstanceId.".concat(index + 1), id];
  })));
}
function flattenTags(tags) {
  return Object.fromEntries(tags.flatMap(function (tag, index) {
    var ordinal = index + 1;
    return [["Tag.".concat(ordinal, ".Key"), tag.Key], ["Tag.".concat(ordinal, ".Value"), tag.Value]];
  }));
}
function normalizeTags(tags) {
  var _tags$Tag;
  return Object.fromEntries(((_tags$Tag = tags === null || tags === void 0 ? void 0 : tags.Tag) !== null && _tags$Tag !== void 0 ? _tags$Tag : []).flatMap(function (tag) {
    var _tag$TagKey, _tag$TagValue;
    var key = (_tag$TagKey = tag.TagKey) !== null && _tag$TagKey !== void 0 ? _tag$TagKey : tag.Key;
    var value = (_tag$TagValue = tag.TagValue) !== null && _tag$TagValue !== void 0 ? _tag$TagValue : tag.Value;
    return key && value ? [[key, value]] : [];
  }));
}
function canonicalizeAliyunParams(params) {
  return Object.keys(params).sort().map(function (key) {
    return "".concat(percentEncode(key), "=").concat(percentEncode(params[key]));
  }).join("&");
}
function percentEncode(value) {
  return encodeURIComponent(value).replace(/\+/g, "%20").replace(/\*/g, "%2A").replace(/%7E/g, "~");
}
function shellQuote(value) {
  return "'".concat(value.replace(/'/g, "'\\''"), "'");
}

export { AliyunEcsRpcClient, HEYUAN_G8I_2XLARGE_RENDER_PROFILE, aliyunWorkerHostId, buildAliyunRunInstancesRequest, buildAliyunWorkerBootstrapUserData, createAliyunEcsClientFromEnv, createAliyunEcsWorkers, deleteAliyunEcsWorkers, listAliyunEcsWorkers, signAliyunRpcRequest };
