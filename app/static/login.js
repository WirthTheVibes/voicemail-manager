// Base path this app is actually served under -- see the matching constant
// in app.js for why (reverse-proxied under a prefix like "/vm-manager" vs.
// hit directly at the domain root).
const BASE = (() => {
  const src = document.currentScript && document.currentScript.src;
  if (!src) return "";
  return new URL(src, window.location.href).pathname.replace(/\/login\.js$/, "");
})();

async function api(path, options = {}) {
  const res = await fetch(BASE + path, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `Request failed: ${res.status}`);
  }
  return res.json();
}

// Where to send the user once they're signed in. Populated by app.js when
// it bounces an unauthenticated deep link (e.g. /app/205/11) here, so a
// pasted/refreshed link lands back where it was headed instead of just the
// mailbox list. Restricted to a same-origin path under BASE so this can't
// be turned into an open redirect via a crafted ?next=.
function resolveNext() {
  const raw = new URLSearchParams(window.location.search).get("next");
  const fallback = `${BASE}/app`;
  if (!raw) return fallback;
  let path;
  try {
    path = decodeURIComponent(raw);
  } catch {
    return fallback;
  }
  if (!path.startsWith(`${BASE}/app`)) return fallback;
  return path;
}
const nextUrl = resolveNext();

// Already signed in (valid session cookie)? Skip straight to the app.
api("/api/me")
  .then(() => { window.location.href = nextUrl; })
  .catch(() => {});

document.getElementById("login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const extension = document.getElementById("login-extension").value.trim();
  const pin = document.getElementById("login-pin").value;
  const errorEl = document.getElementById("login-error");
  errorEl.classList.add("hidden");

  try {
    await api("/api/login", { method: "POST", body: JSON.stringify({ extension, pin }) });
    window.location.href = nextUrl;
  } catch (err) {
    errorEl.textContent = err.message;
    errorEl.classList.remove("hidden");
  }
});

// --- Microsoft (Entra ID) sign-in: Authorization Code + PKCE ----------------
// This is a public-client flow -- no client secret exists anywhere in it.
// The browser generates its own code_verifier/code_challenge, redirects to
// Microsoft, and (in callback.js) exchanges the returned code for tokens
// directly against login.microsoftonline.com. See ms_auth.py for how the
// resulting ID token gets verified server-side.
function base64UrlEncode(buffer) {
  return btoa(String.fromCharCode(...new Uint8Array(buffer)))
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/, "");
}

function randomString(byteLength) {
  const bytes = new Uint8Array(byteLength);
  crypto.getRandomValues(bytes);
  return base64UrlEncode(bytes.buffer);
}

async function sha256(input) {
  return crypto.subtle.digest("SHA-256", new TextEncoder().encode(input));
}

(async () => {
  const msSignInBtn = document.getElementById("ms-signin-btn");
  let authCfg;
  try {
    authCfg = await api("/api/auth-config");
  } catch {
    return; // login page still works for PIN sign-in even if this fails
  }

  if (!authCfg.ms_auth_enabled) return;
  msSignInBtn.classList.remove("hidden");
  if (!authCfg.pin_login_enabled) {
    document.getElementById("login-form").classList.add("hidden");
  }

  msSignInBtn.addEventListener("click", async () => {
    const verifier = randomString(64);
    const challenge = base64UrlEncode(await sha256(verifier));
    const state = randomString(16);
    sessionStorage.setItem("ms_auth_verifier", verifier);
    sessionStorage.setItem("ms_auth_state", state);
    sessionStorage.setItem("ms_auth_next", nextUrl);

    const authorizeUrl = new URL(`https://login.microsoftonline.com/${authCfg.ms_tenant_id}/oauth2/v2.0/authorize`);
    authorizeUrl.search = new URLSearchParams({
      client_id: authCfg.ms_client_id,
      response_type: "code",
      redirect_uri: `${window.location.origin}${BASE}/callback.html`,
      response_mode: "query",
      scope: "openid profile email",
      code_challenge: challenge,
      code_challenge_method: "S256",
      state,
    }).toString();
    window.location.href = authorizeUrl.toString();
  });
})();
