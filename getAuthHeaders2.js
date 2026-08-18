function getAuthHeaders() {
  const tmp = closure_0;
  const tmp2 = closure_1;
  const sessionToken = closure_0(closure_1[2]).getImperativeAuthStore().sessionToken;
  let obj = { headers: null };
  obj = {};
  const tmp3 = closure_2;
  if (sessionToken) {
    if (typeof tmp3 !== "function") {
      HermesBuiltin.throwTypeError();
    }
    const obj1 = { "User-Agent": null, "Accept-Language": null };
    obj1[0] = tmp(tmp2[0]).AppUserAgent;
    obj1[1] = tmp(tmp2[1]).getLocaleImperative();
    const merged = Object.assign(obj1);
    const _HermesInternal = HermesInternal;
    obj.Authorization = "Bearer " + sessionToken;
    obj[0] = obj;
    return obj;
  } else {
    if (typeof tmp3 !== "function") {
      HermesBuiltin.throwTypeError();
    }
    const obj2 = { "User-Agent": null, "Accept-Language": null };
    obj2[0] = tmp(tmp2[0]).AppUserAgent;
    obj2[1] = tmp(tmp2[1]).getLocaleImperative();
    const merged1 = Object.assign(obj2);
    obj[0] = obj;
    return obj;
  }
}

// ========================================
// Expansion summary: 1 functions decompiled
// Root: F33259, Max depth: 2
// ========================================
