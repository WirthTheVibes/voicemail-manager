// Completes the Microsoft (Entra ID) PKCE flow started in login.js: takes
// the ?code returned by Microsoft, exchanges it directly against
// login.microsoftonline.com's /token endpoint (no client secret -- this is
// a public client, see login.js), then hands the resulting ID token (plus
// the Graph-scoped access token, for the proxyAddresses alias fallback) to
// vm-manager's own /api/login/ms to get a session cookie.
const BASE = (() => {
  const src = document.currentScript && document.currentScript.src;
  if (!src) return "";
  return new URL(src, window.location.href).pathname.replace(/\/callback\.js$/, "");
})();

const statusEl = document.getElementById("callback-status");

function showError(message) {
  statusEl.textContent = message;
  statusEl.classList.add("error-text");
  const link = document.createElement("a");
  link.href = `${BASE}/`;
  link.textContent = "Back to sign-in";
  statusEl.after(link);
}

(async () => {
  const params = new URLSearchParams(window.location.search);
  const oauthError = params.get("error_description") || params.get("error");
  if (oauthError) {
    showError(oauthError);
    return;
  }

  const code = params.get("code");
  const state = params.get("state");
  const expectedState = sessionStorage.getItem("ms_auth_state");
  const verifier = sessionStorage.getItem("ms_auth_verifier");
  const next = sessionStorage.getItem("ms_auth_next") || `${BASE}/app`;
  sessionStorage.removeItem("ms_auth_state");
  sessionStorage.removeItem("ms_auth_verifier");
  sessionStorage.removeItem("ms_auth_next");

  if (!code || !verifier || !state || state !== expectedState) {
    showError("Sign-in failed: this link is invalid or expired. Please try again.");
    return;
  }

  try {
    const authCfg = await fetch(`${BASE}/api/auth-config`).then((r) => r.json());

    const tokenRes = await fetch(`https://login.microsoftonline.com/${authCfg.ms_tenant_id}/oauth2/v2.0/token`, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({
        client_id: authCfg.ms_client_id,
        grant_type: "authorization_code",
        code,
        redirect_uri: `${window.location.origin}${BASE}/callback.html`,
        code_verifier: verifier,
        scope: "openid profile email User.Read",
      }),
    });
    const tokenBody = await tokenRes.json();
    if (!tokenRes.ok) {
      throw new Error(tokenBody.error_description || "Microsoft sign-in failed.");
    }

    const loginRes = await fetch(`${BASE}/api/login/ms`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id_token: tokenBody.id_token, access_token: tokenBody.access_token }),
    });
    if (!loginRes.ok) {
      const body = await loginRes.json().catch(() => ({}));
      throw new Error(body.detail || "Sign-in failed.");
    }

    window.location.href = next;
  } catch (err) {
    showError(err.message);
  }
})();
