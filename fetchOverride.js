function f78471(arg0, arg1) {
  if (closure_5 === 2) {
    closure_5 = 3;
    HermesBuiltin.throwTypeError();
  } else if (tmp4 === 3) {
    if (arg0 === 1) {
      throw arg1;
    } else if (arg0 === 2) {
      let obj = { value: null, done: true };
      obj[0] = arg1;
      return obj;
    } else {
      return { value: "T", done: "/assets/assets/images" };
    }
  } else {
    try {
      closure_5 = 2;
      if (0 === closure_4) {
        closure_3 = tmp2;
        closure_2 = tmp5;
        const tmp13 = closure_1;
        closure_0 = undefined;
        closure_1 = undefined;
        closure_2 = undefined;
        if (closure_1_0(closure_1_1[3]).isSimulatedfOffline()) {
          const _TypeError = TypeError;
          const typeError = new TypeError("Network request failed: SIMULATE_OFFLINE - check env file");
          throw typeError;
        } else {
          closure_0 = closure_1_5(tmp12);
          const obj1 = {};
          const authHeaders = closure_1_0(closure_1_1[4]).getAuthHeaders();
          const merged = Object.assign(tmp13);
          let headers;
          if (tmp13 != null) {
            headers = tmp13.headers;
          }
          const obj2 = {};
          const headers1 = new Headers(headers);
          const merged1 = Object.assign(Object.fromEntries(headers1.entries()));
          const merged2 = Object.assign(authHeaders.headers);
          obj1.headers = obj2;
          closure_1 = obj1;
          closure_4 = 1;
          closure_5 = 1;
          const obj3 = { value: null, done: false };
          obj3[0] = closure_1_0(closure_1_1[5]).fetchWithAnonymousCookieBootstrap(/* F81886 */ function() { ... });
          return obj3;
        }
        tmp12 = closure_0;
      } else if (arg0 === 1) {
        closure_5 = 3;
        throw arg1;
      } else if (arg0 === 2) {
        closure_5 = 3;
        const obj4 = { value: null, done: true };
        obj4[0] = arg1;
        return obj4;
      } else {
        closure_2 = arg1;
        const result = closure_0(closure_1[6]).throwIfTailscaleRequired(closure_2);
        closure_5 = 3;
        obj = { value: null, done: true };
        obj[0] = closure_2;
        return obj;
      }
    } catch (tmp41) {
      closure_5 = tmp;
      throw tmp41;
    }
  }
}

// ========================================
// Referenced function F81886
// ========================================

function f81886() {
  return fetch(closure_0, closure_1);
}

// ========================================
// Expansion summary: 2 functions decompiled
// Root: F78471, Max depth: 2
// ========================================
