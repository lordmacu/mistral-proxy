function f77833(arg0) {
  if (closure_3 === 2) {
    closure_3 = 3;
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
      closure_3 = 2;
      if (0 === closure_2) {
        closure_1 = tmp2;
        closure_0 = undefined;
        const tmp8 = closure_1_0;
        const tmp9 = closure_1_1;
        ({ body, signal } = closure_0);
        if (closure_1_0(closure_1_1[0]).isSimulatedfOffline()) {
          const _TypeError = TypeError;
          const typeError = new TypeError("Network request failed: SIMULATE_OFFLINE - check env file");
          throw typeError;
        } else {
          const _HermesInternal = HermesInternal;
          const combined = "" + tmp8(tmp9[1]).getServerBaseUrl() + "/api/chat/resume";
          const authHeaders = tmp8(tmp9[2]).getAuthHeaders();
          const obj1 = { method: "POST", body: null, headers: null, signal: null };
          obj1[1] = tmp8(tmp9[4]).stringify(body);
          const obj2 = { "Content-Type": "application/json", Accept: "text/event-stream" };
          const merged = Object.assign(authHeaders.headers);
          obj1[2] = obj2;
          obj1[3] = signal;
          closure_2 = 1;
          closure_3 = 1;
          const obj3 = { value: null, done: false };
          obj3[0] = tmp8(tmp9[3]).fetchMobileStream(combined, obj1);
          return obj3;
        }
      } else if (arg0 === 1) {
        closure_3 = 3;
        throw arg1;
      } else if (arg0 === 2) {
        closure_3 = 3;
        const obj4 = { value: null, done: true };
        obj4[0] = arg1;
        return obj4;
      } else {
        closure_0 = arg1;
        closure_3 = 3;
        obj = { value: null, done: true };
        obj[0] = closure_0;
        return obj;
      }
    } catch (tmp20) {
      closure_3 = tmp;
      throw tmp20;
    }
  }
}

// ========================================
// Expansion summary: 1 functions decompiled
// Root: F77833, Max depth: 2
// ========================================
