let imports = {};
imports['__wbindgen_placeholder__'] = module.exports;
let wasm;
const { TextEncoder, TextDecoder } = require(`util`);

const heap = new Array(128).fill(undefined);

heap.push(undefined, null, true, false);

function getObject(idx) { return heap[idx]; }

let heap_next = heap.length;

function addHeapObject(obj) {
    if (heap_next === heap.length) heap.push(heap.length + 1);
    const idx = heap_next;
    heap_next = heap[idx];

    heap[idx] = obj;
    return idx;
}

function dropObject(idx) {
    if (idx < 132) return;
    heap[idx] = heap_next;
    heap_next = idx;
}

function takeObject(idx) {
    const ret = getObject(idx);
    dropObject(idx);
    return ret;
}

let WASM_VECTOR_LEN = 0;

let cachedUint8Memory0 = null;

function getUint8Memory0() {
    if (cachedUint8Memory0 === null || cachedUint8Memory0.byteLength === 0) {
        cachedUint8Memory0 = new Uint8Array(wasm.memory.buffer);
    }
    return cachedUint8Memory0;
}

let cachedTextEncoder = new TextEncoder('utf-8');

const encodeString = (typeof cachedTextEncoder.encodeInto === 'function'
    ? function (arg, view) {
    return cachedTextEncoder.encodeInto(arg, view);
}
    : function (arg, view) {
    const buf = cachedTextEncoder.encode(arg);
    view.set(buf);
    return {
        read: arg.length,
        written: buf.length
    };
});

function passStringToWasm0(arg, malloc, realloc) {

    if (realloc === undefined) {
        const buf = cachedTextEncoder.encode(arg);
        const ptr = malloc(buf.length, 1) >>> 0;
        getUint8Memory0().subarray(ptr, ptr + buf.length).set(buf);
        WASM_VECTOR_LEN = buf.length;
        return ptr;
    }

    let len = arg.length;
    let ptr = malloc(len, 1) >>> 0;

    const mem = getUint8Memory0();

    let offset = 0;

    for (; offset < len; offset++) {
        const code = arg.charCodeAt(offset);
        if (code > 0x7F) break;
        mem[ptr + offset] = code;
    }

    if (offset !== len) {
        if (offset !== 0) {
            arg = arg.slice(offset);
        }
        ptr = realloc(ptr, len, len = offset + arg.length * 3, 1) >>> 0;
        const view = getUint8Memory0().subarray(ptr + offset, ptr + len);
        const ret = encodeString(arg, view);

        offset += ret.written;
        ptr = realloc(ptr, len, offset, 1) >>> 0;
    }

    WASM_VECTOR_LEN = offset;
    return ptr;
}

function isLikeNone(x) {
    return x === undefined || x === null;
}

let cachedInt32Memory0 = null;

function getInt32Memory0() {
    if (cachedInt32Memory0 === null || cachedInt32Memory0.byteLength === 0) {
        cachedInt32Memory0 = new Int32Array(wasm.memory.buffer);
    }
    return cachedInt32Memory0;
}

let cachedTextDecoder = new TextDecoder('utf-8', { ignoreBOM: true, fatal: true });

cachedTextDecoder.decode();

function getStringFromWasm0(ptr, len) {
    ptr = ptr >>> 0;
    return cachedTextDecoder.decode(getUint8Memory0().subarray(ptr, ptr + len));
}

let cachedUint32Memory0 = null;

function getUint32Memory0() {
    if (cachedUint32Memory0 === null || cachedUint32Memory0.byteLength === 0) {
        cachedUint32Memory0 = new Uint32Array(wasm.memory.buffer);
    }
    return cachedUint32Memory0;
}

function getArrayU32FromWasm0(ptr, len) {
    ptr = ptr >>> 0;
    return getUint32Memory0().subarray(ptr / 4, ptr / 4 + len);
}
/**
* @param {string} json
* @returns {DefGraph}
*/
module.exports.loadDefGraphFromJson = function(json) {
    const ptr0 = passStringToWasm0(json, wasm.__wbindgen_malloc, wasm.__wbindgen_realloc);
    const len0 = WASM_VECTOR_LEN;
    const ret = wasm.loadDefGraphFromJson(ptr0, len0);
    return DefGraph.__wrap(ret);
};

/**
* @param {string} spec_name
* @param {string | undefined} [unique_spec_label]
* @returns {string}
*/
module.exports.genSpecIdentifier = function(spec_name, unique_spec_label) {
    let deferred3_0;
    let deferred3_1;
    try {
        const retptr = wasm.__wbindgen_add_to_stack_pointer(-16);
        const ptr0 = passStringToWasm0(spec_name, wasm.__wbindgen_malloc, wasm.__wbindgen_realloc);
        const len0 = WASM_VECTOR_LEN;
        var ptr1 = isLikeNone(unique_spec_label) ? 0 : passStringToWasm0(unique_spec_label, wasm.__wbindgen_malloc, wasm.__wbindgen_realloc);
        var len1 = WASM_VECTOR_LEN;
        wasm.genSpecIdentifier(retptr, ptr0, len0, ptr1, len1);
        var r0 = getInt32Memory0()[retptr / 4 + 0];
        var r1 = getInt32Memory0()[retptr / 4 + 1];
        deferred3_0 = r0;
        deferred3_1 = r1;
        return getStringFromWasm0(r0, r1);
    } finally {
        wasm.__wbindgen_add_to_stack_pointer(16);
        wasm.__wbindgen_free(deferred3_0, deferred3_1, 1);
    }
};

/**
* @param {UniqueStreamIdentifierParams} p
* @returns {string}
*/
module.exports.uniqueStreamIdentifier = function(p) {
    let deferred1_0;
    let deferred1_1;
    try {
        const retptr = wasm.__wbindgen_add_to_stack_pointer(-16);
        wasm.uniqueStreamIdentifier(retptr, addHeapObject(p));
        var r0 = getInt32Memory0()[retptr / 4 + 0];
        var r1 = getInt32Memory0()[retptr / 4 + 1];
        deferred1_0 = r0;
        deferred1_1 = r1;
        return getStringFromWasm0(r0, r1);
    } finally {
        wasm.__wbindgen_add_to_stack_pointer(16);
        wasm.__wbindgen_free(deferred1_0, deferred1_1, 1);
    }
};

function handleError(f, args) {
    try {
        return f.apply(this, args);
    } catch (e) {
        wasm.__wbindgen_exn_store(addHeapObject(e));
    }
}

const DefGraphFinalization = (typeof FinalizationRegistry === 'undefined')
    ? { register: () => {}, unregister: () => {} }
    : new FinalizationRegistry(ptr => wasm.__wbg_defgraph_free(ptr >>> 0));
/**
*/
class DefGraph {

    static __wrap(ptr) {
        ptr = ptr >>> 0;
        const obj = Object.create(DefGraph.prototype);
        obj.__wbg_ptr = ptr;
        DefGraphFinalization.register(obj, obj.__wbg_ptr, obj);
        return obj;
    }

    __destroy_into_raw() {
        const ptr = this.__wbg_ptr;
        this.__wbg_ptr = 0;
        DefGraphFinalization.unregister(this);
        return ptr;
    }

    free() {
        const ptr = this.__destroy_into_raw();
        wasm.__wbg_defgraph_free(ptr);
    }
    /**
    * @param {DefGraphParams} params
    */
    constructor(params) {
        try {
            const retptr = wasm.__wbindgen_add_to_stack_pointer(-16);
            wasm.defgraph_new(retptr, addHeapObject(params));
            var r0 = getInt32Memory0()[retptr / 4 + 0];
            var r1 = getInt32Memory0()[retptr / 4 + 1];
            var r2 = getInt32Memory0()[retptr / 4 + 2];
            if (r2) {
                throw takeObject(r1);
            }
            this.__wbg_ptr = r0 >>> 0;
            return this;
        } finally {
            wasm.__wbindgen_add_to_stack_pointer(16);
        }
    }
    /**
    * @returns {string}
    */
    toJson() {
        let deferred1_0;
        let deferred1_1;
        try {
            const retptr = wasm.__wbindgen_add_to_stack_pointer(-16);
            wasm.defgraph_toJson(retptr, this.__wbg_ptr);
            var r0 = getInt32Memory0()[retptr / 4 + 0];
            var r1 = getInt32Memory0()[retptr / 4 + 1];
            deferred1_0 = r0;
            deferred1_1 = r1;
            return getStringFromWasm0(r0, r1);
        } finally {
            wasm.__wbindgen_add_to_stack_pointer(16);
            wasm.__wbindgen_free(deferred1_0, deferred1_1, 1);
        }
    }
    /**
    * @returns {Uint32Array}
    */
    getSpecNodeIds() {
        try {
            const retptr = wasm.__wbindgen_add_to_stack_pointer(-16);
            wasm.defgraph_getSpecNodeIds(retptr, this.__wbg_ptr);
            var r0 = getInt32Memory0()[retptr / 4 + 0];
            var r1 = getInt32Memory0()[retptr / 4 + 1];
            var v1 = getArrayU32FromWasm0(r0, r1).slice();
            wasm.__wbindgen_free(r0, r1 * 4, 4);
            return v1;
        } finally {
            wasm.__wbindgen_add_to_stack_pointer(16);
        }
    }
    /**
    * @returns {number}
    */
    getRootSpecNodeId() {
        const ret = wasm.defgraph_getRootSpecNodeId(this.__wbg_ptr);
        return ret >>> 0;
    }
    /**
    * @param {number} node_id
    * @returns {DefGraphNode}
    */
    getNodeAttributes(node_id) {
        const ret = wasm.defgraph_getNodeAttributes(this.__wbg_ptr, node_id);
        return takeObject(ret);
    }
    /**
    * @param {number} node_id
    * @returns {GetInboundNodeSetsResult}
    */
    getInboundNodeSets(node_id) {
        const ret = wasm.defgraph_getInboundNodeSets(this.__wbg_ptr, node_id);
        return takeObject(ret);
    }
    /**
    * @param {number} node_id
    * @returns {GetOutboundNodeSetsResult}
    */
    getOutboundNodeSets(node_id) {
        const ret = wasm.defgraph_getOutboundNodeSets(this.__wbg_ptr, node_id);
        return takeObject(ret);
    }
    /**
    * @returns {Uint32Array}
    */
    nodes() {
        try {
            const retptr = wasm.__wbindgen_add_to_stack_pointer(-16);
            wasm.defgraph_nodes(retptr, this.__wbg_ptr);
            var r0 = getInt32Memory0()[retptr / 4 + 0];
            var r1 = getInt32Memory0()[retptr / 4 + 1];
            var v1 = getArrayU32FromWasm0(r0, r1).slice();
            wasm.__wbindgen_free(r0, r1 * 4, 4);
            return v1;
        } finally {
            wasm.__wbindgen_add_to_stack_pointer(16);
        }
    }
    /**
    * @param {number} node_id
    * @returns {Uint32Array}
    */
    inboundNeighbors(node_id) {
        try {
            const retptr = wasm.__wbindgen_add_to_stack_pointer(-16);
            wasm.defgraph_inboundNeighbors(retptr, this.__wbg_ptr, node_id);
            var r0 = getInt32Memory0()[retptr / 4 + 0];
            var r1 = getInt32Memory0()[retptr / 4 + 1];
            var v1 = getArrayU32FromWasm0(r0, r1).slice();
            wasm.__wbindgen_free(r0, r1 * 4, 4);
            return v1;
        } finally {
            wasm.__wbindgen_add_to_stack_pointer(16);
        }
    }
    /**
    * @param {number} node_id
    * @returns {Uint32Array}
    */
    outboundNeighbors(node_id) {
        try {
            const retptr = wasm.__wbindgen_add_to_stack_pointer(-16);
            wasm.defgraph_outboundNeighbors(retptr, this.__wbg_ptr, node_id);
            var r0 = getInt32Memory0()[retptr / 4 + 0];
            var r1 = getInt32Memory0()[retptr / 4 + 1];
            var v1 = getArrayU32FromWasm0(r0, r1).slice();
            wasm.__wbindgen_free(r0, r1 * 4, 4);
            return v1;
        } finally {
            wasm.__wbindgen_add_to_stack_pointer(16);
        }
    }
    /**
    * @returns {EdgesResults}
    */
    edges() {
        const ret = wasm.defgraph_edges(this.__wbg_ptr);
        return takeObject(ret);
    }
    /**
    * @param {FromSpecAndTag} from
    * @param {ToSpecAndTag} to
    */
    addConnectedDualSpecs(from, to) {
        wasm.defgraph_addConnectedDualSpecs(this.__wbg_ptr, addHeapObject(from), addHeapObject(to));
    }
    /**
    * @param {SpecAndTag} s
    */
    ensureOutletAndStream(s) {
        wasm.defgraph_ensureOutletAndStream(this.__wbg_ptr, addHeapObject(s));
    }
    /**
    * @param {SpecAndTag} s
    * @param {boolean} has_transform
    */
    ensureInletAndStream(s, has_transform) {
        wasm.defgraph_ensureInletAndStream(this.__wbg_ptr, addHeapObject(s), has_transform);
    }
    /**
    * @param {AssignAliasParams} p
    */
    assignAlias(p) {
        wasm.defgraph_assignAlias(this.__wbg_ptr, addHeapObject(p));
    }
    /**
    * @returns {Uint32Array}
    */
    getAllAliasNodeIds() {
        try {
            const retptr = wasm.__wbindgen_add_to_stack_pointer(-16);
            wasm.defgraph_getAllAliasNodeIds(retptr, this.__wbg_ptr);
            var r0 = getInt32Memory0()[retptr / 4 + 0];
            var r1 = getInt32Memory0()[retptr / 4 + 1];
            var v1 = getArrayU32FromWasm0(r0, r1).slice();
            wasm.__wbindgen_free(r0, r1 * 4, 4);
            return v1;
        } finally {
            wasm.__wbindgen_add_to_stack_pointer(16);
        }
    }
    /**
    * @param {LookUpRootSpecAliasParams} p
    * @returns {string | undefined}
    */
    lookupRootSpecAlias(p) {
        try {
            const retptr = wasm.__wbindgen_add_to_stack_pointer(-16);
            wasm.defgraph_lookupRootSpecAlias(retptr, this.__wbg_ptr, addHeapObject(p));
            var r0 = getInt32Memory0()[retptr / 4 + 0];
            var r1 = getInt32Memory0()[retptr / 4 + 1];
            let v1;
            if (r0 !== 0) {
                v1 = getStringFromWasm0(r0, r1).slice();
                wasm.__wbindgen_free(r0, r1 * 1, 1);
            }
            return v1;
        } finally {
            wasm.__wbindgen_add_to_stack_pointer(16);
        }
    }
    /**
    * @param {string} alias
    * @param {string} direction
    * @returns {SpecAndTagInfoAndDirection | undefined}
    */
    lookupSpecAndTagByAlias(alias, direction) {
        const ptr0 = passStringToWasm0(alias, wasm.__wbindgen_malloc, wasm.__wbindgen_realloc);
        const len0 = WASM_VECTOR_LEN;
        const ptr1 = passStringToWasm0(direction, wasm.__wbindgen_malloc, wasm.__wbindgen_realloc);
        const len1 = WASM_VECTOR_LEN;
        const ret = wasm.defgraph_lookupSpecAndTagByAlias(this.__wbg_ptr, ptr0, len0, ptr1, len1);
        return takeObject(ret);
    }
}
module.exports.DefGraph = DefGraph;

module.exports.__wbindgen_object_clone_ref = function(arg0) {
    const ret = getObject(arg0);
    return addHeapObject(ret);
};

module.exports.__wbindgen_object_drop_ref = function(arg0) {
    takeObject(arg0);
};

module.exports.__wbindgen_is_undefined = function(arg0) {
    const ret = getObject(arg0) === undefined;
    return ret;
};

module.exports.__wbg_new_abda76e883ba8a5f = function() {
    const ret = new Error();
    return addHeapObject(ret);
};

module.exports.__wbg_stack_658279fe44541cf6 = function(arg0, arg1) {
    const ret = getObject(arg1).stack;
    const ptr1 = passStringToWasm0(ret, wasm.__wbindgen_malloc, wasm.__wbindgen_realloc);
    const len1 = WASM_VECTOR_LEN;
    getInt32Memory0()[arg0 / 4 + 1] = len1;
    getInt32Memory0()[arg0 / 4 + 0] = ptr1;
};

module.exports.__wbg_error_f851667af71bcfc6 = function(arg0, arg1) {
    let deferred0_0;
    let deferred0_1;
    try {
        deferred0_0 = arg0;
        deferred0_1 = arg1;
        console.error(getStringFromWasm0(arg0, arg1));
    } finally {
        wasm.__wbindgen_free(deferred0_0, deferred0_1, 1);
    }
};

module.exports.__wbg_parse_66d1801634e099ac = function() { return handleError(function (arg0, arg1) {
    const ret = JSON.parse(getStringFromWasm0(arg0, arg1));
    return addHeapObject(ret);
}, arguments) };

module.exports.__wbg_stringify_8887fe74e1c50d81 = function() { return handleError(function (arg0) {
    const ret = JSON.stringify(getObject(arg0));
    return addHeapObject(ret);
}, arguments) };

module.exports.__wbindgen_string_get = function(arg0, arg1) {
    const obj = getObject(arg1);
    const ret = typeof(obj) === 'string' ? obj : undefined;
    var ptr1 = isLikeNone(ret) ? 0 : passStringToWasm0(ret, wasm.__wbindgen_malloc, wasm.__wbindgen_realloc);
    var len1 = WASM_VECTOR_LEN;
    getInt32Memory0()[arg0 / 4 + 1] = len1;
    getInt32Memory0()[arg0 / 4 + 0] = ptr1;
};

module.exports.__wbindgen_throw = function(arg0, arg1) {
    throw new Error(getStringFromWasm0(arg0, arg1));
};

const path = require('path').join(__dirname, 'livestack_shared_wasm_nodejs_bg.wasm');
const bytes = require('fs').readFileSync(path);

const wasmModule = new WebAssembly.Module(bytes);
const wasmInstance = new WebAssembly.Instance(wasmModule, imports);
wasm = wasmInstance.exports;
module.exports.__wasm = wasm;

