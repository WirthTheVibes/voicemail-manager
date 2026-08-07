const ALL_MAILBOXES = "__all__";
const MESSAGES_PAGE_SIZE = 15;

// Base path this app is actually served under -- "" when hit directly at
// http://host:8080/, but e.g. "/vm-manager" when reverse-proxied at
// https://your-pbx.3cx.ca/vm-manager/ (see the nginx snippet that
// forwards that prefix here, stripping it before it reaches FastAPI --
// this backend never sees it). Derived from this very script's own
// resolved URL rather than hardcoded, so the same file works under either
// deployment without a build step or server-side templating.
const BASE = (() => {
  const src = document.currentScript && document.currentScript.src;
  if (!src) return "";
  return new URL(src, window.location.href).pathname.replace(/\/app\.js$/, "");
})();
const APP_ROOT = `${BASE}/app`;
const SETTINGS_PATH = `${APP_ROOT}/settings`;

const state = {
  me: null,
  mailboxes: [],
  currentExtension: ALL_MAILBOXES,
  messages: [],
  messagesPage: 1,
  currentMessageId: null,
  groups: [],
  sidebarSort: "ext", // "ext" or "alpha" — secondary sort key, after department
  usersSort: "ext", // "ext" or "alpha" — Settings > Users list sort
  departmentsSort: "alpha", // "alpha" or "size" — Settings > Departments list sort
  viewMode: "voicemail", // "voicemail" or "calls" — which table is showing for currentExtension
  calls: [],
  callsPage: 1,
  currentCounterpart: null, // counterpart_key of the selected row's counterpart (drives the detail-panel fetch)
  currentCallId: null, // the specific clicked row's own id -- drives which single row highlights (see renderCallLog)
};

// This page (app.html) is a real, separate document from the login page
// (index.html) — see login.js for why: password managers get confused when
// a login form's password field lingers hidden-but-present in the DOM
// across a JS-only "login", so auth here is a genuine browser navigation
// in both directions instead of a visibility toggle.

async function api(path, options = {}) {
  // `path` is always written as if this app owned the domain root
  // (e.g. "/api/me") -- BASE gets prepended here so the same call works
  // whether that's literally true or this is reverse-proxied under a
  // prefix (see BASE above).
  const res = await fetch(BASE + path, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  if (res.status === 401) {
    const next = encodeURIComponent(window.location.pathname + window.location.search);
    window.location.href = `${BASE}/?next=${next}`;
    const err = new Error("Not authenticated");
    err.status = 401;
    err.isAuthRedirect = true;
    throw err;
  }
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    const err = new Error(body.detail || `Request failed: ${res.status}`);
    err.status = res.status;
    throw err;
  }
  if (res.status === 204) return null;
  return res.json();
}

function el(id) { return document.getElementById(id); }

// --- URL routing ---
// The whole point: every view reachable from the sidebar/settings gets a
// real URL under /app, pushed via history.pushState, so a link copied out
// of the address bar reopens the same view for whoever it's sent to
// (see the server-side catch-all in main.py that serves app.html for any
// /app/* path so a hard refresh or pasted link doesn't 404).
//
// mailbox view:   /app                          -> own extension
//                 /app/all                       -> "view all voicemails"
//                 /app/<ext>                     -> that mailbox
// settings view:  /app/settings                  -> settings modal, Users tab
//                 /app/settings/users/<ext>       -> Users tab, that user selected
//                 /app/settings/departments/<id>  -> Departments tab, that dept selected

// viewMode "calls" builds /app/<ext>/calls[/<counterpart>] instead of the
// voicemail-mode /app/<ext>[/<messageId>] -- messageId is ignored in calls
// mode (counterpart takes its slot instead, see selectCounterpart).
function mailboxPath(extension, messageId = null, viewMode = "voicemail", counterpart = null) {
  const base = `${APP_ROOT}/${extension === ALL_MAILBOXES ? "all" : encodeURIComponent(extension)}`;
  if (viewMode === "calls") {
    return counterpart ? `${base}/calls/${encodeURIComponent(counterpart)}` : `${base}/calls`;
  }
  return messageId ? `${base}/${encodeURIComponent(messageId)}` : base;
}

// Per-message link for the share modal's "Copy link" button -- opens
// straight to a single voicemail (mailbox + detail pane), scoped to the
// mailbox it lives in so the server's normal per-mailbox access check
// (see loadMessages) is what decides whether the recipient can see it.
function messagePath(extension, messageId) {
  return mailboxPath(extension, messageId);
}

function parseRoute(pathname) {
  let path = pathname.startsWith(APP_ROOT) ? pathname.slice(APP_ROOT.length) : pathname;
  path = path.replace(/^\/+|\/+$/g, "");
  const parts = path ? path.split("/").map(decodeURIComponent) : [];

  if (parts[0] === "settings") {
    if (parts[1] === "users" && parts[2]) return { view: "settings", tab: "users", selected: parts[2] };
    if (parts[1] === "departments" && parts[2]) return { view: "settings", tab: "departments", selected: parts[2] };
    if (parts[1]) return { view: "settings", tab: parts[1], selected: null };
    return { view: "settings", tab: "general", selected: null };
  }
  if (parts[0]) {
    const extension = parts[0] === "all" ? ALL_MAILBOXES : parts[0];
    if (parts[1] === "calls") {
      return { view: "mailbox", extension, viewMode: "calls", counterpart: parts[2] || null, messageId: null };
    }
    return { view: "mailbox", extension, viewMode: "voicemail", messageId: parts[1] || null };
  }
  return { view: "mailbox", extension: null, viewMode: "voicemail", messageId: null }; // null = fill in own extension at boot/apply time
}

function setUrl(path, { replace = false } = {}) {
  if (replace) history.replaceState(null, "", path);
  else history.pushState(null, "", path);
}

// Re-derives the visible view from the current URL. Used for the initial
// load and for back/forward (popstate) -- NOT for clicks, which already
// know what they want to show and call setUrl() themselves alongside their
// normal click-handling logic, to avoid this re-running a full settings
// re-render on top of a click that just did the equivalent work directly.
async function applyRoute(route) {
  if (route.view === "settings") {
    await openSettingsOverlay(route.tab, route.selected);
  } else {
    closeSettingsOverlay();
    await selectMailbox(route.extension || state.me.extension, route.messageId, route.viewMode, route.counterpart);
  }
}

// --- error / permission modal ---
function openErrorModal(title, message) {
  el("error-modal-title").textContent = title;
  el("error-modal-body").textContent = message;
  el("error-modal-overlay").classList.remove("hidden");
}

function closeErrorModal() {
  el("error-modal-overlay").classList.add("hidden");
}

function showApiErrorModal(err) {
  if (!err || err.isAuthRedirect) return; // api() is already redirecting to "/"
  const status = err.status;
  const title = status === 403 ? "Permission Denied" : status === 404 ? "Not Found" : "Error";
  const detail = err.message || "Something went wrong.";
  openErrorModal(title, status ? `HTTP ${status} — ${detail}` : detail);
}

el("error-modal-close").addEventListener("click", closeErrorModal);

document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if (!el("error-modal-overlay").classList.contains("hidden")) {
    closeErrorModal();
    return;
  }
  if (!el("share-modal-overlay").classList.contains("hidden")) {
    closeShareModal();
    return;
  }
  if (!el("settings-overlay").classList.contains("hidden")) {
    setUrl(mailboxPath(state.currentExtension));
    closeSettingsOverlay();
  }
});

function initials(firstname, lastname) {
  return `${(firstname || "").charAt(0)}${(lastname || "").charAt(0)}`.toUpperCase() || "?";
}

function initialsFromName(name) {
  const parts = (name || "").trim().split(/\s+/).filter(Boolean);
  if (parts.length === 0) return "?";
  if (parts.length === 1) return parts[0].charAt(0).toUpperCase();
  return (parts[0].charAt(0) + parts[parts.length - 1].charAt(0)).toUpperCase();
}

const REVIEW_CHECK_ICON = `
  <svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true">
    <circle cx="8" cy="8" r="8" fill="currentColor"/>
    <path d="M4.5 8.3L6.8 10.6L11.5 5.6" stroke="white" stroke-width="1.4" fill="none" stroke-linecap="round" stroke-linejoin="round"/>
  </svg>
`;

// Detail-panel toggle icons (heard / reviewed) share one shape: a base glyph
// that gains a diagonal strike-through when the toggled state is "on" — the
// strike reads as "no longer needs your attention", not a checkmark.
function crossableIcon(innerMarkup, crossed) {
  return `
    <svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true">
      ${innerMarkup}
      ${crossed ? '<line x1="1.5" y1="14.5" x2="14.5" y2="1.5" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/>' : ""}
    </svg>
  `;
}

const EAR_ICON_INNER = `
  <path d="M6.5 2.5C4 2.5 2 4.5 2 7c0 1.3.6 2.4 1.5 3.2.6.5.9 1.1.9 1.8v.5" stroke="currentColor" stroke-width="1.3" fill="none" stroke-linecap="round"/>
  <path d="M6.5 5.3c-1 0-1.7.8-1.7 1.7 0 .6.3 1.1.7 1.4" stroke="currentColor" stroke-width="1.1" fill="none" stroke-linecap="round"/>
  <path d="M10 5a4 4 0 0 1 0 5.5" stroke="currentColor" stroke-width="1.1" fill="none" stroke-linecap="round"/>
  <path d="M12 3.3a6.3 6.3 0 0 1 0 8.4" stroke="currentColor" stroke-width="1.1" fill="none" stroke-linecap="round"/>
`;

const TRASH_ICON_INNER = `
  <path d="M2.5 4.3h11M6 4.3V2.8c0-.5.4-.9.9-.9h2.2c.5 0 .9.4.9.9v1.5M4.7 4.3l.6 8.4c0 .5.5.9 1 .9h3.4c.5 0 .9-.4 1-.9l.6-8.4" stroke="currentColor" stroke-width="1.2" fill="none" stroke-linecap="round" stroke-linejoin="round"/>
`;

// The familiar "box with an arrow escaping upward" share glyph (iOS-style),
// drawn in the same thin-stroke look as the ear/trash icons above.
const SHARE_ICON = `
  <svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true">
    <path d="M8 1.5v7.8M5.3 4.2L8 1.5l2.7 2.7" stroke="currentColor" stroke-width="1.2" fill="none" stroke-linecap="round" stroke-linejoin="round"/>
    <path d="M3 7v6.2c0 .7.6 1.3 1.3 1.3h7.4c.7 0 1.3-.6 1.3-1.3V7" stroke="currentColor" stroke-width="1.2" fill="none" stroke-linecap="round" stroke-linejoin="round"/>
  </svg>
`;

// Playback-mode toggle (below the player): speaker = play in the browser
// (existing <audio> element), phone = ring the viewer's own extension and
// play the WAV down the call (see POST api/messages/{id}/call).
const SPEAKER_ICON = `
  <svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true">
    <path d="M2 6.1h2.1L7 3.7v8.6L4.1 9.9H2z" fill="currentColor"/>
    <path d="M9.4 5.2a3.3 3.3 0 0 1 0 5.6" stroke="currentColor" stroke-width="1.1" fill="none" stroke-linecap="round"/>
    <path d="M11.1 3.5a5.8 5.8 0 0 1 0 9" stroke="currentColor" stroke-width="1.1" fill="none" stroke-linecap="round"/>
  </svg>
`;
// The universal filled "call" glyph (same path used by Material Icons /
// Ionicons' phone icon) -- native viewBox is 24x24, rendered at 16x16.
const PHONE_ICON = `
  <svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true">
    <path fill="currentColor" d="M6.62 10.79c1.44 2.83 3.76 5.14 6.59 6.59l2.2-2.2c.27-.27.67-.36 1.02-.24 1.12.37 2.33.57 3.57.57.55 0 1 .45 1 1V20c0 .55-.45 1-1 1-9.39 0-17-7.61-17-17 0-.55.45-1 1-1h3.5c.55 0 1 .45 1 1 0 1.25.2 2.45.57 3.57.11.35.03.74-.25 1.02l-2.2 2.2z"/>
  </svg>
`;

// "Called back" badge beside a reviewer's checkmark (see REVIEW_CHECK_ICON
// above) -- same circle-badge treatment: solid blue fill with a white glyph.
const CALLBACK_BADGE_ICON = `
  <svg viewBox="-3 -3 30 30" width="16" height="16" aria-hidden="true">
    <circle cx="12" cy="12" r="14" fill="currentColor"/>
    <path fill="white" d="M6.62 10.79c1.44 2.83 3.76 5.14 6.59 6.59l2.2-2.2c.27-.27.67-.36 1.02-.24 1.12.37 2.33.57 3.57.57.55 0 1 .45 1 1V20c0 .55-.45 1-1 1-9.39 0-17-7.61-17-17 0-.55.45-1 1-1h3.5c.55 0 1 .45 1 1 0 1.25.2 2.45.57 3.57.11.35.03.74-.25 1.02l-2.2 2.2z"/>
  </svg>
`;

// Call Log direction column: a missed call still renders with the inbound
// arrow (it *was* an incoming call, just unanswered) -- redness comes from
// the status dot, not this icon. See routes/calls.py:_format_call.
const DIRECTION_IN_ICON = `
  <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
    <path d="M4 4l6 6M10 4v6H4" stroke="currentColor" stroke-width="1.4" fill="none" stroke-linecap="round" stroke-linejoin="round"/>
  </svg>
`;
const DIRECTION_OUT_ICON = `
  <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
    <path d="M4 10l6-6M4 4h6v6" stroke="currentColor" stroke-width="1.4" fill="none" stroke-linecap="round" stroke-linejoin="round"/>
  </svg>
`;

function reviewedByMe(m) {
  return (m.reviewers || []).some((r) => r.extension === state.me.extension && r.reviewed_at);
}

// A message counts as reviewed either by listening to it (wireCustomPlayer's
// markReviewedOnce) or, for the transcription-reading case where no audio
// ever plays, by just leaving it open a while (see the dwell tracker below).
function markReviewed(m) {
  return api(`/api/messages/${m.id}/reviewed`, { method: "POST" })
    .then((result) => {
      m.reviewers = result.reviewers;
      if (state.currentMessageId === m.id) renderReviewList(m);
    })
    .catch((err) => console.warn("mark reviewed failed", err));
}

// Debug-only counterpart to markReviewed (Ctrl+click on the heard button) --
// removes the current user's own review row so the reviewed-tracking system
// can be tested both ways without touching app_db by hand.
function unmarkReviewed(m) {
  return api(`/api/messages/${m.id}/reviewed`, { method: "DELETE" })
    .then((result) => {
      m.reviewers = result.reviewers;
      if (state.currentMessageId === m.id) renderReviewList(m);
    })
    .catch((err) => console.warn("unmark reviewed failed", err));
}

// Patches the heard icon + status text in place (no full renderDetail,
// which would tear down and restart the audio element mid-playback).
function updateHeardIcon(m) {
  const btn = el("toggle-heard-btn");
  if (btn) {
    btn.classList.toggle("active", m.heard);
    btn.title = `${m.heard ? "Mark unheard" : "Mark heard"}`;
    btn.innerHTML = crossableIcon(EAR_ICON_INNER, m.heard);
  }
  const status = el("detail-status-value");
  if (status) status.textContent = m.heard ? "Heard" : "New";
}

// Dwell-based auto-review: reading a transcript doesn't touch the audio
// element, so plays-based tracking alone would never mark it reviewed.
// Instead, note when a message's detail view opens, and if the user has
// navigated away (a different message, a different mailbox) after at least
// this long, treat that as "read it and moved on".
const DETAIL_REVIEW_DWELL_MS = 7000; // user asked for "5/10 seconds" -- splitting the difference; tune here.
let detailDwellState = null; // { message, openedAt } for whatever's currently open

function noteDetailOpened(m) {
  detailDwellState = { message: m, openedAt: Date.now() };
}

function flushDetailDwellReview() {
  if (!detailDwellState) return;
  const { message, openedAt } = detailDwellState;
  detailDwellState = null;
  if (reviewedByMe(message)) return;
  if (Date.now() - openedAt < DETAIL_REVIEW_DWELL_MS) return;
  markReviewed(message);
}

function fullName(firstname, lastname) {
  return [firstname, lastname].filter(Boolean).join(" ");
}

// Ext-number pill + name, matching the sidebar mailbox row's look (see
// renderMailboxRow) — used everywhere else an extension/name pair shows up
// so the two styles don't drift apart.
function extChip(ext) {
  return `<span class="ext-chip">${escapeHtml(ext)}</span>`;
}
function pillName(ext, name) {
  return `<span class="pill-name">${extChip(ext)}<span class="pill-name-text">${escapeHtml(name)}</span></span>`;
}

el("logout-btn").addEventListener("click", async () => {
  await api("/api/logout", { method: "POST" }).catch(() => {});
  window.location.href = `${BASE}/`; // not "." -- deep mailbox/message URLs make "current directory" wrong
});

el("refresh-btn").addEventListener("click", () => loadMailboxes());

// --- Push notifications: per-device opt-in ------------------------------------
// Independent of the org-wide Settings > Notifications "PWA enabled" switch —
// that switch controls whether the server ever sends; this controls whether
// *this* browser is one of the devices it could send to. Stays hidden
// entirely if the browser has no Push API or service-worker registration
// fails (e.g. this server has no VAPID keys configured yet).
function urlBase64ToUint8Array(base64) {
  const padding = "=".repeat((4 - (base64.length % 4)) % 4);
  const base64Safe = (base64 + padding).replace(/-/g, "+").replace(/_/g, "/");
  const raw = atob(base64Safe);
  return Uint8Array.from([...raw].map((c) => c.charCodeAt(0)));
}

async function getPushSubscription() {
  const registration = await navigator.serviceWorker.register(`${BASE}/sw.js`, { scope: `${BASE}/` });
  return registration.pushManager.getSubscription();
}

function updateNotifyButton(subscribed) {
  const btn = el("notify-btn");
  btn.classList.toggle("active", subscribed);
  btn.title = subscribed
    ? "Voicemail notifications enabled on this device (click to disable)"
    : "Enable voicemail notifications on this device";
}

async function initPushButton() {
  if (!("serviceWorker" in navigator) || !("PushManager" in window)) return;

  const btn = el("notify-btn");
  let existing;
  try {
    existing = await getPushSubscription();
  } catch {
    return; // registration failed -- leave the button hidden rather than offer something broken
  }
  btn.classList.remove("hidden");
  updateNotifyButton(!!existing);

  btn.addEventListener("click", async () => {
    btn.disabled = true;
    try {
      const current = await getPushSubscription();
      if (current) {
        await current.unsubscribe();
        await api("/api/push/subscribe", {
          method: "DELETE",
          body: JSON.stringify({ endpoint: current.endpoint }),
        }).catch(() => {});
        updateNotifyButton(false);
        return;
      }

      const permission = await Notification.requestPermission();
      if (permission !== "granted") return;

      const { publicKey } = await api("/api/push/vapid-public-key");
      const registration = await navigator.serviceWorker.ready;
      const subscription = await registration.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: urlBase64ToUint8Array(publicKey),
      });
      await api("/api/push/subscribe", { method: "POST", body: JSON.stringify(subscription.toJSON()) });
      updateNotifyButton(true);
    } catch (err) {
      showApiErrorModal(err);
    } finally {
      btn.disabled = false;
    }
  });
}

// Wraps selectMailbox() with the URL push a real click should cause -- see
// applyRoute(), which calls selectMailbox() directly (no setUrl) when the
// URL already reflects the destination, e.g. on popstate or initial load.
function navigateToMailbox(extension, viewMode = "voicemail", counterpart = null) {
  setUrl(mailboxPath(extension, null, viewMode, counterpart));
  selectMailbox(extension, null, viewMode, counterpart);
}

el("view-all-link").addEventListener("click", (e) => {
  e.preventDefault();
  navigateToMailbox(ALL_MAILBOXES);
});

// Self-view (requirement 1): a static, always-visible Voicemails/Call Log
// switch below the "me" identity row -- not a menu, both options are always
// shown with their own badge, whichever is current is highlighted. This is
// the only way to switch tables for one's own mailbox -- there's no visible
// tab strip for self (see #mailbox-view-tabs, hidden whenever
// currentExtension === me.extension).
function renderSidebarUserPopover() {
  const own = state.mailboxes.find((b) => b.extension === state.me.extension);
  const unread = own ? own.unread : 0;
  const unreadBadge = el("popover-unread-badge");
  unreadBadge.textContent = unread;
  unreadBadge.classList.toggle("hidden", unread === 0);
  const isSelf = state.currentExtension === state.me.extension;
  el("sidebar-user-popover")
    .querySelectorAll(".user-popover-option")
    .forEach((btn) => btn.classList.toggle("active", isSelf && state.viewMode === btn.dataset.mode));
}

el("sidebar-user").addEventListener("click", () => {
  if (state.me) navigateToMailbox(state.me.extension);
});

el("sidebar-user-popover").querySelectorAll(".user-popover-option").forEach((btn) => {
  btn.addEventListener("click", () => navigateToMailbox(state.me.extension, btn.dataset.mode));
});

el("sidebar-search").addEventListener("input", () => renderMailboxes());

el("sidebar-sort-toggle").addEventListener("click", () => {
  state.sidebarSort = state.sidebarSort === "ext" ? "alpha" : "ext";
  updateSidebarSortToggle();
  renderMailboxes();
});

function updateSidebarSortToggle() {
  const alpha = state.sidebarSort === "alpha";
  el("sidebar-sort-icon").textContent = alpha ? "A" : "1";
  el("sidebar-sort-toggle").title = alpha ? "Sorted A–Z — click to sort by extension" : "Sorted by extension — click to sort A–Z";
}

// --- boot ---
async function boot() {
  try {
    state.me = await api("/api/me");
  } catch {
    return; // api() already redirected to "/"
  }
  el("nav-user").textContent = `${state.me.extension} · ${fullName(state.me.firstname, state.me.lastname)}`;
  el("settings-btn").classList.toggle("hidden", !state.me.is_admin);
  initPushButton();
  connectEvents();

  const route = parseRoute(location.pathname);
  state.currentExtension = route.view === "mailbox" && route.extension ? route.extension : state.me.extension;
  state.viewMode = route.viewMode || "voicemail";
  updateSidebarSortToggle();
  const ok = await loadMailboxes();

  if (route.view === "settings") {
    await openSettingsOverlay(route.tab, route.selected);
  } else if (state.viewMode === "calls" && route.counterpart && ok) {
    selectCounterpart(route.counterpart);
  } else if (route.messageId && ok) {
    const message = state.messages.find((msg) => String(msg.id) === String(route.messageId));
    if (message) {
      selectMessage(message.id);
    } else {
      showApiErrorModal({ status: 404, message: "This voicemail doesn't exist or isn't in this mailbox." });
    }
  }

  window.addEventListener("popstate", () => applyRoute(parseRoute(location.pathname)));
}

function renderSidebarUser() {
  el("nav-user").textContent = `${state.me.extension} · ${fullName(state.me.firstname, state.me.lastname)}`;
  const own = state.mailboxes.find((b) => b.extension === state.me.extension);
  const unread = own ? own.unread : 0;
  const button = el("sidebar-user");
  button.classList.toggle("active", state.currentExtension === state.me.extension);
  button.innerHTML = `
    <div class="avatar-circle">${initials(state.me.firstname, state.me.lastname)}</div>
    <div class="sidebar-user-info">
      <div class="ext">Ext. ${escapeHtml(state.me.extension)}</div>
      <div class="name">${escapeHtml(fullName(state.me.firstname, state.me.lastname))}</div>
    </div>
    ${unread > 0 ? `<span class="badge">${unread}</span>` : ""}
  `;
  renderSidebarUserPopover();
}

// --- mailboxes ---
async function loadMailboxes() {
  state.mailboxes = await api("/api/mailboxes");
  renderSidebarUser();
  renderMailboxes();
  return await loadCurrentView(state.currentExtension);
}

// Dispatches to whichever table is currently showing -- voicemail (existing
// behavior) or the new Call Log. Also updates the tab strip/thead to match.
function loadCurrentView(extension) {
  updateViewTabs();
  return state.viewMode === "calls" ? loadCallLog(extension) : loadMessages(extension);
}

function renderMailboxRow(box) {
  const row = document.createElement("div");
  row.className = "mailbox-row" + (box.extension === state.currentExtension ? " active" : "");
  const name = box.name || box.extension;
  row.innerHTML = `
    <span class="mailbox-label" title="${escapeHtml(box.extension)} — ${escapeHtml(name)}">
      <span class="ext-chip">${escapeHtml(box.extension)}</span>
      <span class="mailbox-name">${escapeHtml(name)}</span>
    </span>
    ${box.unread > 0 ? `<span class="badge">${box.unread}</span>` : ""}
  `;
  row.addEventListener("click", () => navigateToMailbox(box.extension));
  return row;
}

// Department the mailbox is grouped under for sorting: for a group mailbox
// (designated in the Departments tab) that's the department that claimed it
// via its "department:X" source if this viewer sees it that way, otherwise
// its own department; for everything else it's the extension's own
// department.
function departmentOf(box) {
  return box.is_group && box.source.startsWith("department:")
    ? box.source.slice("department:".length)
    : box.department || "";
}

// Sidebar order is always department-first, then either extension number or
// name, per the sort toggle in the search box.
function compareMailboxes(a, b) {
  const deptA = departmentOf(a).toLowerCase();
  const deptB = departmentOf(b).toLowerCase();
  if (deptA !== deptB) return deptA < deptB ? -1 : 1;
  if (state.sidebarSort === "alpha") {
    const nameA = (a.name || a.extension).toLowerCase();
    const nameB = (b.name || b.extension).toLowerCase();
    if (nameA !== nameB) return nameA < nameB ? -1 : 1;
    return 0;
  }
  return parseInt(a.extension, 10) - parseInt(b.extension, 10);
}

// Fills `container` with the given (already-sorted) mailboxes, inserting a
// department sub-heading each time the department changes. Group mailboxes
// skip the sub-headings (`showDeptHeadings = false`) since each one usually
// *is* a department, making a heading redundant with the row itself.
function renderMailboxGroup(container, boxes, showDeptHeadings = true) {
  container.innerHTML = "";
  let lastDept = null;
  boxes.forEach((box) => {
    if (showDeptHeadings) {
      const dept = departmentOf(box) || "Unassigned";
      if (dept !== lastDept) {
        const heading = document.createElement("div");
        heading.className = "sidebar-subheading";
        heading.textContent = dept;
        container.appendChild(heading);
        lastDept = dept;
      }
    }
    container.appendChild(renderMailboxRow(box));
  });
}

function renderMailboxes() {
  el("view-all-link").classList.toggle("active", state.currentExtension === ALL_MAILBOXES);

  const searchTerm = el("sidebar-search").value.trim().toLowerCase();
  const matches = (box) =>
    !searchTerm || box.extension.toLowerCase().includes(searchTerm) || (box.name || "").toLowerCase().includes(searchTerm);

  // Own extension is represented by the primary sidebar-user button above,
  // so it's excluded here to avoid listing it twice. Mailboxes the user sees
  // because their department owns them (Settings -> Departments tab) are
  // pinned above everything else under "Group Mailboxes" instead of "Mailboxes".
  const others = state.mailboxes.filter((b) => b.extension !== state.me.extension && matches(b));
  const groupMailboxes = others.filter((b) => b.is_group).sort(compareMailboxes);
  const individualMailboxes = others.filter((b) => !b.is_group).sort(compareMailboxes);

  el("group-section").classList.toggle("hidden", groupMailboxes.length === 0);
  renderMailboxGroup(el("group-mailbox-list"), groupMailboxes, false);

  el("individual-section").classList.toggle("hidden", individualMailboxes.length === 0);
  renderMailboxGroup(el("mailbox-list"), individualMailboxes);
}

// `messageId` reproduces a per-message share link (see openShareModal):
// after the mailbox's messages load, jump straight to that message's detail
// pane, same as clicking its row.
async function selectMailbox(extension, messageId = null, viewMode = "voicemail", counterpart = null) {
  closeSettingsOverlay(); // defensive: covers popstate arriving from a settings URL
  flushDetailDwellReview();
  state.currentExtension = extension;
  state.viewMode = viewMode;
  state.currentCounterpart = null;
  state.currentCallId = null;
  renderSidebarUser();
  renderMailboxes();
  const ok = await loadCurrentView(extension);
  if (viewMode === "calls") {
    if (counterpart && ok) selectCounterpart(counterpart);
    return;
  }
  if (messageId && ok) {
    const message = state.messages.find((msg) => String(msg.id) === String(messageId));
    if (message) {
      selectMessage(message.id);
    } else {
      showApiErrorModal({ status: 404, message: "This voicemail doesn't exist or isn't in this mailbox." });
    }
  }
}

// --- messages ---
async function loadMessages(extension) {
  state.messagesPage = 1;
  try {
    state.messages =
      extension === ALL_MAILBOXES
        ? await api("/api/mailboxes/all/messages")
        : await api(`/api/mailboxes/${encodeURIComponent(extension)}/messages`);
  } catch (err) {
    // e.g. a link to someone else's mailbox they don't have access to --
    // show why instead of leaving a blank/broken message list.
    state.messages = [];
    showApiErrorModal(err);
    renderMessages();
    return false;
  }
  renderMessages();
  return true;
}

// Local (viewer-timezone) "YYYY-MM-DD" key for a Date -- matches the format
// a <input type="date"> value comes back in, so the two can be compared with
// plain string comparison for the date-range filter below.
function localDateKey(d) {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

function renderMessages() {
  const tbody = el("message-rows");
  // "All voicemails" is the only view mixing messages from several mailboxes,
  // so it's the only one where the From column's DID line needs to say which
  // mailbox each message landed in.
  const showMailboxColumn = state.currentExtension === ALL_MAILBOXES;

  const filterTerm = el("search-input").value.trim().toLowerCase();
  const dateFrom = el("search-date-from").value; // "" or "YYYY-MM-DD"
  const dateTo = el("search-date-to").value;

  const filtered = state.messages.filter((m) => {
    if (filterTerm && !(m.crm_name || m.caller_name || "").toLowerCase().includes(filterTerm) && !(m.caller || "").includes(filterTerm)) {
      return false;
    }
    if (dateFrom || dateTo) {
      const d = parseServerTimestamp(m.created_time);
      if (!d) return false;
      const key = localDateKey(d);
      if (dateFrom && key < dateFrom) return false;
      if (dateTo && key > dateTo) return false;
    }
    return true;
  });

  if (filtered.length === 0) {
    tbody.innerHTML = `<tr><td class="col-status-dot"></td><td colspan="4" style="opacity:0.5; padding: var(--space-4);">No messages</td></tr>`;
    renderPagination(0, 1, 1);
    return;
  }

  const totalPages = Math.max(1, Math.ceil(filtered.length / MESSAGES_PAGE_SIZE));
  if (state.messagesPage > totalPages) state.messagesPage = totalPages;
  if (state.messagesPage < 1) state.messagesPage = 1;
  const pageStart = (state.messagesPage - 1) * MESSAGES_PAGE_SIZE;
  const pageItems = filtered.slice(pageStart, pageStart + MESSAGES_PAGE_SIZE);

  tbody.innerHTML = "";
  for (const m of pageItems) {
    const tr = document.createElement("tr");
    tr.className = [m.id === state.currentMessageId ? "active" : "", m.heard ? "row-reviewed" : "row-new"]
      .filter(Boolean)
      .join(" ");
    // In "All voicemails", the DID line also names the mailbox the message
    // landed in -- e.g. "4039486660 - (301 Jeremy Matthews)" -- since one
    // caller number can show up across several people's mailboxes there.
    const callerFormatted = formatPhone(m.caller || "");
    const didLine = showMailboxColumn
      ? `${callerFormatted}${m.caller ? " - " : ""}(${m.mailbox_extension} ${m.mailbox_name || ""})`
      : callerFormatted;
    // "Reviewed by" only means something once more than one person can see
    // this mailbox -- for a single-reviewer (personal) mailbox it's left
    // blank rather than hidden, so the column itself stays put across every
    // view instead of appearing/disappearing as you switch mailboxes.
    const hasMultipleReviewers = (m.reviewers || []).length > 1;
    const firstReviewerText = hasMultipleReviewers && m.first_reviewer ? escapeHtml(m.first_reviewer.name) : "";
    tr.innerHTML = `
      <td class="col-status-dot"><span class="status-dot ${m.heard ? "status-dot-reviewed" : "status-dot-new"}" title="${m.heard ? "Reviewed" : "New"}"></span></td>
      <td class="col-from">${escapeHtml(m.crm_name || m.caller_name || "Unknown Caller")}<br><span style="opacity:0.5; font-size:12px;">${escapeHtml(didLine)}</span></td>
      <td class="col-received">${formatTimestamp(m.created_time)}</td>
      <td class="col-duration">${formatDurationMs(m.duration)}</td>
      <td class="col-first-reviewer">${firstReviewerText}</td>
    `;
    tr.addEventListener("click", () => selectMessage(m.id));
    tbody.appendChild(tr);
  }

  renderPagination(filtered.length, state.messagesPage, totalPages);
}

function renderPagination(total, page, totalPages) {
  const container = el("message-pagination");
  if (!container) return;
  if (total === 0) {
    container.innerHTML = "";
    return;
  }
  const rangeStart = (page - 1) * MESSAGES_PAGE_SIZE + 1;
  const rangeEnd = Math.min(page * MESSAGES_PAGE_SIZE, total);
  container.innerHTML = `
    <span class="pagination-summary">${rangeStart}&ndash;${rangeEnd} of ${total}</span>
    <div class="pagination-controls">
      <button type="button" class="btn btn-secondary" id="pagination-prev" ${page <= 1 ? "disabled" : ""}>Prev</button>
      <span class="pagination-page">Page ${page} of ${totalPages}</span>
      <button type="button" class="btn btn-secondary" id="pagination-next" ${page >= totalPages ? "disabled" : ""}>Next</button>
    </div>
  `;
  el("pagination-prev").addEventListener("click", () => {
    if (state.viewMode === "calls") state.callsPage -= 1;
    else state.messagesPage -= 1;
    renderCurrentTable();
  });
  el("pagination-next").addEventListener("click", () => {
    if (state.viewMode === "calls") state.callsPage += 1;
    else state.messagesPage += 1;
    renderCurrentTable();
  });
}

// Dispatches to whichever table's renderer applies right now -- shared by
// renderPagination's prev/next handlers and the search/date filter
// listeners below, both of which are wired once and need to stay correct
// across a Voicemail <-> Call Log switch (see updateViewTabs/the sidebar
// popover, both of which flip state.viewMode without re-wiring these).
function renderCurrentTable() {
  if (state.viewMode === "calls") renderCallLog();
  else renderMessages();
}

// Any change to the search/date filters can shrink the result set out from
// under the page the user was on, so these reset to page 1 rather than
// leaving them stranded on a now-empty page.
el("search-input").addEventListener("input", () => {
  state.messagesPage = 1;
  state.callsPage = 1;
  renderCurrentTable();
});
el("search-date-from").addEventListener("change", () => {
  state.messagesPage = 1;
  state.callsPage = 1;
  renderCurrentTable();
});
el("search-date-to").addEventListener("change", () => {
  state.messagesPage = 1;
  state.callsPage = 1;
  renderCurrentTable();
});
el("search-date-clear").addEventListener("click", () => {
  el("search-date-from").value = "";
  el("search-date-to").value = "";
  state.messagesPage = 1;
  state.callsPage = 1;
  renderCurrentTable();
});

// --- view tabs (Voicemail / Call Log) for someone else's individual mailbox --
// Self-view switches via the sidebar-user popover only (see above) -- this
// strip is hidden for state.currentExtension === state.me.extension, for
// ALL_MAILBOXES, and for any group mailbox (requirement 2: group mailboxes
// never get a Call Log view at all).
function currentBox() {
  return state.mailboxes.find((b) => b.extension === state.currentExtension);
}

function updateViewTabs() {
  const box = currentBox();
  const eligible =
    state.currentExtension !== state.me.extension && state.currentExtension !== ALL_MAILBOXES && box && !box.is_group;
  el("mailbox-view-tabs").classList.toggle("hidden", !eligible);
  if (eligible) {
    el("mailbox-view-tabs")
      .querySelectorAll(".tab-btn")
      .forEach((b) => b.classList.toggle("active", b.dataset.view === state.viewMode));
  }
  setMessageTableHead(state.viewMode);
}

el("mailbox-view-tabs")
  .querySelectorAll(".tab-btn")
  .forEach((btn) => {
    btn.addEventListener("click", () => {
      state.viewMode = btn.dataset.view;
      state.currentCounterpart = null;
      state.currentCallId = null;
      setUrl(mailboxPath(state.currentExtension, null, state.viewMode), { replace: true });
      loadCurrentView(state.currentExtension);
    });
  });

function setMessageTableHead(mode) {
  const head = el("message-table-head");
  head.innerHTML =
    mode === "calls"
      ? `<tr>
          <th class="col-status-dot"></th>
          <th class="col-direction"></th>
          <th class="col-from">Name/CallerID</th>
          <th class="col-number">Number</th>
          <th class="col-received">Date</th>
        </tr>`
      : `<tr>
          <th class="col-status-dot"></th>
          <th class="col-from">From</th>
          <th class="col-received">Received</th>
          <th class="col-duration">Duration</th>
          <th class="col-first-reviewer">Reviewed by</th>
        </tr>`;
}

// --- call log ---
async function loadCallLog(extension) {
  state.callsPage = 1;
  try {
    state.calls = await api(`/api/mailboxes/${encodeURIComponent(extension)}/calls`);
  } catch (err) {
    state.calls = [];
    showApiErrorModal(err);
    renderCallLog();
    return false;
  }
  renderCallLog();
  return true;
}

function statusDotClass(status) {
  return { missed: "status-dot-missed", received: "status-dot-received", outgoing: "status-dot-outgoing" }[status] || "";
}

function statusLabel(status) {
  return { missed: "Missed", received: "Received", outgoing: "Outgoing" }[status] || status;
}

function renderCallLog() {
  const tbody = el("message-rows");

  const filterTerm = el("search-input").value.trim().toLowerCase();
  const dateFrom = el("search-date-from").value;
  const dateTo = el("search-date-to").value;

  const filtered = state.calls.filter((c) => {
    if (
      filterTerm &&
      !(c.party_name || "").toLowerCase().includes(filterTerm) &&
      !(c.counterpart_key || "").includes(filterTerm)
    ) {
      return false;
    }
    if (dateFrom || dateTo) {
      const d = parseCallTimestamp(c.start_time);
      if (!d) return false;
      const key = localDateKey(d);
      if (dateFrom && key < dateFrom) return false;
      if (dateTo && key > dateTo) return false;
    }
    return true;
  });

  if (filtered.length === 0) {
    tbody.innerHTML = `<tr><td class="col-status-dot"></td><td colspan="4" style="opacity:0.5; padding: var(--space-4);">No calls</td></tr>`;
    renderPagination(0, 1, 1);
    return;
  }

  const totalPages = Math.max(1, Math.ceil(filtered.length / MESSAGES_PAGE_SIZE));
  if (state.callsPage > totalPages) state.callsPage = totalPages;
  if (state.callsPage < 1) state.callsPage = 1;
  const pageStart = (state.callsPage - 1) * MESSAGES_PAGE_SIZE;
  const pageItems = filtered.slice(pageStart, pageStart + MESSAGES_PAGE_SIZE);

  tbody.innerHTML = "";
  for (const c of pageItems) {
    const tr = document.createElement("tr");
    // Highlight only the specific row that was clicked (its own call id),
    // not every row that happens to share the same counterpart -- the
    // detail pane groups by counterpart, but the table selection shouldn't.
    tr.className = c.id === state.currentCallId ? "active" : "";
    tr.innerHTML = `
      <td class="col-status-dot"><span class="status-dot ${statusDotClass(c.status)}" title="${statusLabel(c.status)}"></span></td>
      <td class="col-direction">${c.direction === "out" ? DIRECTION_OUT_ICON : DIRECTION_IN_ICON}</td>
      <td class="col-from">${escapeHtml(c.party_name || formatPhone(c.counterpart_key || "") || "Unknown")}</td>
      <td class="col-number">${escapeHtml(formatPhone(c.counterpart_key || ""))}</td>
      <td class="col-received">${formatCallTimestamp(c.start_time)}</td>
    `;
    tr.addEventListener("click", () => selectCounterpart(c.counterpart_key, c.id));
    tbody.appendChild(tr);
  }

  renderPagination(filtered.length, state.callsPage, totalPages);
}

// --- counterpart call-history detail pane (requirement 4) ---
async function selectCounterpart(counterpartKey, callId = null) {
  state.currentCounterpart = counterpartKey;
  state.currentCallId = callId;
  renderCallLog();
  setUrl(mailboxPath(state.currentExtension, null, "calls", counterpartKey), { replace: true });
  let rows;
  try {
    rows = await api(`/api/mailboxes/${encodeURIComponent(state.currentExtension)}/calls/${encodeURIComponent(counterpartKey)}`);
  } catch (err) {
    showApiErrorModal(err);
    return;
  }
  renderCounterpartDetail(counterpartKey, rows);
}

function dayLabel(d) {
  const now = new Date();
  const startOfDay = (dt) => new Date(dt.getFullYear(), dt.getMonth(), dt.getDate()).getTime();
  const dayDiff = Math.round((startOfDay(now) - startOfDay(d)) / 86400000);
  if (dayDiff === 0) return "Today";
  if (dayDiff === 1) return "Yesterday";
  if (dayDiff < 7) return d.toLocaleDateString(undefined, { weekday: "long" });
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function renderCallHistoryList(rows) {
  const container = el("call-history-list");
  container.innerHTML = "";
  let lastLabel = null;
  for (const c of rows) {
    const d = parseCallTimestamp(c.start_time);
    const label = d ? dayLabel(d) : "";
    if (label !== lastLabel) {
      const heading = document.createElement("div");
      heading.className = "detail-section-heading call-history-day-heading";
      heading.textContent = label;
      container.appendChild(heading);
      lastLabel = label;
    }
    const row = document.createElement("div");
    row.className = "call-history-row";
    row.innerHTML = `
      <span class="status-dot ${statusDotClass(c.status)}" title="${statusLabel(c.status)}"></span>
      <span class="call-history-direction">${c.direction === "out" ? DIRECTION_OUT_ICON : DIRECTION_IN_ICON}</span>
      <span class="call-history-label">${statusLabel(c.status)} Call</span>
      <span class="call-history-time">${d ? d.toLocaleString(undefined, { hour: "numeric", minute: "2-digit" }) : ""}</span>
    `;
    container.appendChild(row);
  }
}

function renderCounterpartDetail(counterpartKey, rows) {
  const panel = el("detail-panel");
  const header = rows[0] || {};
  panel.innerHTML = `
    <div class="detail-section">
      <div class="detail-header">
        <div>
          <div class="detail-name">${escapeHtml(header.party_name || formatPhone(counterpartKey))}</div>
          <div style="opacity:0.6; font-size:12px;">${escapeHtml(formatPhone(counterpartKey))}</div>
        </div>
        <div class="detail-actions">
          <button class="icon-toggle-btn" id="counterpart-call-btn" type="button" title="Call">${PHONE_ICON}</button>
        </div>
      </div>
    </div>
    <div class="detail-section">
      <div class="detail-section-heading">Call History</div>
      <div class="call-history-list" id="call-history-list"></div>
    </div>
  `;
  renderCallHistoryList(rows);

  const callBtn = el("counterpart-call-btn");
  const statusEl = document.createElement("div");
  statusEl.className = "phone-call-status";
  statusEl.id = "counterpart-call-status";
  callBtn.closest(".detail-section").appendChild(statusEl);
  callBtn.addEventListener("click", () => {
    if (callBtn.disabled) return;
    callBtn.disabled = true;
    statusEl.textContent = "Calling your extension…";
    api(`/api/mailboxes/${encodeURIComponent(state.currentExtension)}/calls/${encodeURIComponent(counterpartKey)}/call`, {
      method: "POST",
    })
      .then((result) => {
        statusEl.textContent = `Ringing ext. ${result.extension} — answer to be transferred to ${result.dest}.`;
        setTimeout(() => {
          callBtn.disabled = false;
        }, PHONE_CALL_COOLDOWN_MS);
      })
      .catch((err) => {
        statusEl.textContent = err.message || "Could not place the call.";
        callBtn.disabled = false;
      });
  });
}

function selectMessage(id) {
  if (id !== state.currentMessageId) flushDetailDwellReview();
  state.currentMessageId = id;
  renderMessages();
  const message = state.messages.find((m) => m.id === id);
  renderDetail(message);
  noteDetailOpened(message);
}

// --- custom audio player (no native <audio controls> chrome) ---
const PLAYBACK_MODE_STORAGE_KEY = "vm-playback-mode";
// A successful "call" request only confirms the INVITE was sent, not that
// the phone finished ringing -- re-enabling the button as soon as that
// fetch resolves let repeated clicks queue up several concurrent calls to
// the same extension. Hold the button disabled a bit longer than that.
const PHONE_CALL_COOLDOWN_MS = 3000;

function wireCustomPlayer(m) {
  const audio = el("player-audio");
  const btn = el("player-btn");
  const track = el("player-track");
  const fill = el("player-track-fill");
  const curTimeEl = el("player-time-current");
  const durTimeEl = el("player-time-duration");
  const speakerBtn = el("mode-speaker-btn");
  const phoneBtn = el("mode-phone-btn");
  const phoneStatusEl = el("phone-call-status");

  let markedReviewed = false;
  let markingHeard = false;
  let mode = "speaker"; // "speaker" (browser <audio>) or "phone" (ring my extension) -- see the toggle below
  let phoneStatusTimer = null;
  let placingCall = false;

  function showPhoneStatus(text, clearAfterMs) {
    phoneStatusEl.textContent = text;
    if (phoneStatusTimer) clearTimeout(phoneStatusTimer);
    if (clearAfterMs) {
      phoneStatusTimer = setTimeout(() => { phoneStatusEl.textContent = ""; }, clearAfterMs);
    }
  }

  function setMode(next, persist) {
    mode = next;
    speakerBtn.classList.toggle("active", mode === "speaker");
    speakerBtn.setAttribute("aria-pressed", String(mode === "speaker"));
    phoneBtn.classList.toggle("active", mode === "phone");
    phoneBtn.setAttribute("aria-pressed", String(mode === "phone"));
    btn.title = mode === "phone" ? "Call my phone" : "Play on computer";
    showPhoneStatus("");
    if (persist) {
      try {
        localStorage.setItem(PLAYBACK_MODE_STORAGE_KEY, mode);
      } catch {
        // storage unavailable (private browsing, etc.) -- just don't persist
      }
    }
  }

  speakerBtn.addEventListener("click", () => {
    if (mode !== "speaker") audio.pause();
    setMode("speaker", true);
  });
  phoneBtn.addEventListener("click", () => setMode("phone", true));

  // Restore whichever mode was last picked, so switching to "phone" sticks
  // across messages and page reloads instead of resetting to "speaker" every time.
  let storedMode = null;
  try {
    storedMode = localStorage.getItem(PLAYBACK_MODE_STORAGE_KEY);
  } catch {
    // storage unavailable -- fall back to the "speaker" default below
  }
  if (storedMode === "phone" || storedMode === "speaker") setMode(storedMode, false);

  function placePhoneCall() {
    if (placingCall) return;
    placingCall = true;
    btn.disabled = true;
    showPhoneStatus("Calling your extension…");
    api(`/api/messages/${m.id}/call`, { method: "POST" })
      .then((result) => {
        showPhoneStatus(`Ringing ext. ${result.extension} — pick up to listen.`, 8000);
        // Cooldown, not an immediate re-enable: the request only confirms
        // the INVITE went out, so the extension is still ringing/active
        // well after this resolves.
        placingCall = false;
        setTimeout(() => { btn.disabled = false; }, PHONE_CALL_COOLDOWN_MS);
      })
      .catch((err) => {
        showPhoneStatus(err.message || "Could not place the call.", 8000);
        // No call was actually placed, so let the user retry right away.
        placingCall = false;
        btn.disabled = false;
      });
  }

  function markReviewedOnce() {
    if (markedReviewed) return;
    markedReviewed = true;
    markReviewed(m);
  }

  // Real listening is what should flip 3CX's own heard bit (and fire the
  // native SIP NOTIFY) -- not just the app's own reviewed-tracking. Patches
  // the icon in place rather than calling renderDetail(m), which would tear
  // down and restart the <audio> element mid-playback.
  function markHeardOnce() {
    if (markingHeard || m.heard) return;
    markingHeard = true;
    api(`/api/messages/${m.id}/heard`, { method: "POST", body: JSON.stringify({ heard: true }) })
      .then((result) => {
        // A hidden reviewer's listen is audited but intentionally left
        // un-flipped server-side (see routes/messages.py:set_heard), so
        // reflect whatever the server actually did, not what we asked for.
        m.heard = result.heard;
        updateHeardIcon(m);
        renderMessages();
        return loadMailboxes();
      })
      .catch((err) => {
        markingHeard = false; // native-notify has no DB fallback -- allow a retry on the next timeupdate tick
        console.warn("mark heard failed", err);
      });
  }

  function showPlayerError(message) {
    btn.disabled = true;
    track.style.cursor = "default";
    const errEl = el("player-error");
    if (errEl) errEl.textContent = message;
  }

  btn.addEventListener("click", () => {
    if (mode === "phone") {
      placePhoneCall();
      return;
    }
    if (audio.paused) {
      audio.play().catch((err) => {
        console.warn("playback failed", err);
        showPlayerError("This voicemail's audio is no longer available.");
      });
    } else {
      audio.pause();
    }
  });

  audio.addEventListener("play", () => { btn.textContent = "⏸"; });
  audio.addEventListener("pause", () => { btn.textContent = "▶"; });

  audio.addEventListener("error", () => {
    showPlayerError("This voicemail's audio is no longer available.");
  });

  audio.addEventListener("loadedmetadata", () => {
    durTimeEl.textContent = formatSeconds(audio.duration);
  });

  audio.addEventListener("timeupdate", () => {
    if (audio.duration) {
      fill.style.width = `${(audio.currentTime / audio.duration) * 100}%`;
    }
    curTimeEl.textContent = formatSeconds(audio.currentTime);
    if (audio.currentTime > 1) {
      markReviewedOnce();
      markHeardOnce();
    }
  });

  track.addEventListener("click", (e) => {
    const rect = track.getBoundingClientRect();
    const ratio = Math.min(Math.max((e.clientX - rect.left) / rect.width, 0), 1);
    if (audio.duration) {
      audio.currentTime = ratio * audio.duration;
    }
  });
}

function formatSeconds(seconds) {
  const s = Math.floor(Number(seconds) || 0);
  const m = Math.floor(s / 60);
  const rem = s % 60;
  return `${m}:${String(rem).padStart(2, "0")}`;
}

function renderReviewList(m) {
  const container = el("review-list");
  if (!container) return;
  const reviewers = m.reviewers || [];
  if (reviewers.length === 0) {
    container.innerHTML = `<div style="font-size:12px; opacity:0.5;">No one has access to this mailbox yet.</div>`;
    return;
  }
  const callbacks = m.callbacks || {};
  container.innerHTML = reviewers
    .map((r) => {
      const calledBackAt = callbacks[r.extension];
      return `
      <div class="review-row">
        <div class="avatar-circle sm">${escapeHtml(initialsFromName(r.name))}</div>
        <div class="review-name">${escapeHtml(r.name)}</div>
        ${
          r.reviewed_at
            ? `<div class="review-status" title="Reviewed at ${escapeHtml(formatTimestamp(r.reviewed_at))}">
                 <span class="review-check">${REVIEW_CHECK_ICON}</span>
                 ${
                   calledBackAt
                     ? `<span class="review-callback" title="Called back at ${escapeHtml(formatCallTimestamp(calledBackAt))}">${CALLBACK_BADGE_ICON}</span>`
                     : ""
                 }
                 <span>${escapeHtml(formatTimestamp(r.reviewed_at))}</span>
               </div>`
            : `<div class="review-status pending">Pending</div>`
        }
      </div>
    `;
    })
    .join("");
}

// Fetches, for the currently open message, which reviewers called the
// caller back after reviewing (blue phone icon beside their checkmark in
// renderReviewList). Kept separate from the reviewers list itself since
// it's its own on-demand endpoint (see routes/messages.py:message_callbacks)
// -- avoids a call-log lookup per reviewer for every message in the list.
function loadReviewCallbacks(m) {
  if (!(m.reviewers || []).some((r) => r.reviewed_at)) return;
  api(`/api/messages/${m.id}/callbacks`)
    .then((result) => {
      m.callbacks = result.callbacks;
      if (state.currentMessageId === m.id) renderReviewList(m);
    })
    .catch((err) => console.warn("load callbacks failed", err));
}

// Renders the "Call path" section from routes/messages.py's call_path
// endpoint (see threecx_db.call_path) -- one node per hop the call actually
// passed through, chained by arrows. The first node is the real calling
// number (origin), not the trunk DN -- every leg of an inbound call shares
// the same trunk source (e.g. "10000 Telnyx"), which isn't a routing step
// worth showing, so the backend swaps it for source_participant_phone_number
// before this ever sees it.
function renderCallPath(m) {
  const container = el("call-path-list");
  if (!container) return;
  const path = m.callPath;
  if (!path || !path.origin || path.hops.length === 0) {
    container.innerHTML = `<div style="font-size:12px; opacity:0.5;">No call path recorded for this message.</div>`;
    return;
  }
  const nodeLabel = (num, name) => {
    const n = formatPhone((num || "").toString().trim());
    if (!n) return name ? escapeHtml(name) : "Unknown";
    return name
      ? `${escapeHtml(n)} <span class="call-path-node-name">${escapeHtml(name)}</span>`
      : escapeHtml(n);
  };
  const nodes = [nodeLabel(path.origin.number, path.origin.name)];
  path.hops.forEach((h) => nodes.push(nodeLabel(h.number, h.name)));
  container.innerHTML = `<div class="call-path-chain">${nodes
    .map((n, i) => `<span class="call-path-node">${n}</span>${i < nodes.length - 1 ? `<span class="call-path-arrow">&rarr;</span>` : ""}`)
    .join("")}</div>`;
}

function loadCallPath(m) {
  api(`/api/messages/${m.id}/call-path`)
    .then((result) => {
      m.callPath = result;
      if (state.currentMessageId === m.id) renderCallPath(m);
    })
    .catch((err) => {
      console.warn("load call path failed", err);
      const container = el("call-path-list");
      if (container) container.innerHTML = `<div style="font-size:12px; opacity:0.5;">Call path unavailable.</div>`;
    });
}

// Groups flat word-level timestamps into sentences (split after any word
// ending in ./!/?), so the transcript can highlight a whole sentence at a
// time -- reads better mid-playback than a single word jumping around --
// while each word inside stays individually clickable for exact seeking.
// A trailing sentence with no terminal punctuation (transcript cut off
// mid-thought) still gets flushed as its own group.
function groupWordsIntoSentences(words) {
  const sentences = [];
  let current = [];
  for (const w of words) {
    current.push(w);
    if (/[.!?]$/.test(w.word)) {
      sentences.push(current);
      current = [];
    }
  }
  if (current.length) sentences.push(current);
  return sentences;
}

// Word-level timestamps (m.transcription_words, from faster-whisper locally
// or OpenAI's whisper-1 -- see app/transcription.py) turn the transcript
// into clickable spans: click a word to seek the player there exactly, and
// the whole sentence under the playhead highlights as it plays (see
// wireTranscriptWords). Falls back to plain, unclickable text when word
// timestamps aren't available (e.g. the OpenAI engine on its default
// model) or there's no transcription yet.
function renderTranscriptionBody(m) {
  if (!m.transcription) {
    return `<div class="reserved-slot">No transcription yet</div>
            <button class="btn btn-secondary" id="generate-transcription-btn" type="button">Generate transcription</button>`;
  }
  if (m.transcription_words && m.transcription_words.length) {
    const sentences = groupWordsIntoSentences(m.transcription_words)
      .map((sentence) => {
        const words = sentence
          .map((w) => `<span class="transcript-word" data-start="${w.start}" data-end="${w.end}">${escapeHtml(w.word)}</span>`)
          .join(" ");
        const start = sentence[0].start;
        const end = sentence[sentence.length - 1].end;
        return `<span class="transcript-sentence" data-start="${start}" data-end="${end}">${words}</span>`;
      })
      .join(" ");
    return `<div class="transcription-text" id="transcription-text">${sentences}</div>`;
  }
  return `<div class="transcription-text">${escapeHtml(m.transcription)}</div>`;
}

function wireTranscriptWords(m) {
  const container = el("transcription-text");
  if (!container) return;
  const audio = el("player-audio");
  const words = Array.from(container.querySelectorAll(".transcript-word"));
  const sentences = Array.from(container.querySelectorAll(".transcript-sentence"));
  if (!words.length) return;

  words.forEach((span) => {
    span.addEventListener("click", () => {
      const start = parseFloat(span.dataset.start);
      if (Number.isNaN(start)) return;
      const speakerBtn = el("mode-speaker-btn");
      if (speakerBtn && !speakerBtn.classList.contains("active")) speakerBtn.click();
      audio.currentTime = start;
      audio.play().catch(() => {}); // autoplay can be blocked -- the word still seeks either way
    });
  });

  let activeSentence = null;
  audio.addEventListener("timeupdate", () => {
    const t = audio.currentTime;
    const next = sentences.find((span) => t >= parseFloat(span.dataset.start) && t < parseFloat(span.dataset.end));
    if (next === activeSentence) return;
    if (activeSentence) activeSentence.classList.remove("active");
    if (next) {
      next.classList.add("active");
      next.scrollIntoView({ block: "nearest", behavior: "smooth" });
    }
    activeSentence = next || null;
  });
}

function renderDetail(m) {
  const panel = el("detail-panel");
  const heardTitle = m.heard ? "Mark unheard" : "Mark listened";
  // Group mailboxes (Departments tab) restrict deletion to Supervisor/
  // Manager/System Owner -- see access.can_delete_from_mailbox. can_delete
  // rides on the mailbox object from /api/mailboxes, not on the message.
  const mailboxBox = state.mailboxes.find((b) => b.extension === m.callee);
  const canDelete = !mailboxBox || mailboxBox.can_delete;
  panel.innerHTML = `
    <div class="detail-section">
      <div class="detail-header">
        <div>
          <div class="detail-name">${escapeHtml(m.crm_name || m.caller_name || "Unknown Caller")}</div>
          <div style="opacity:0.6; font-size:12px;">${escapeHtml(formatPhone(m.caller || ""))}</div>
        </div>
        <div class="detail-actions">
          <button class="icon-toggle-btn" id="callback-btn" type="button" title="Call back">
            ${PHONE_ICON}
          </button>
          <button class="icon-toggle-btn ${m.heard ? "active" : ""}" id="toggle-heard-btn" type="button" title="${heardTitle}">
            ${crossableIcon(EAR_ICON_INNER, m.heard)}
          </button>
          <button class="icon-toggle-btn" id="share-btn" type="button" title="Share">
            ${SHARE_ICON}
          </button>
          <button class="icon-toggle-btn" id="delete-btn" type="button" title="${canDelete ? "Delete" : "Only a Supervisor or Manager can delete from this mailbox"}" ${canDelete ? "" : "disabled"}>
            ${crossableIcon(TRASH_ICON_INNER, false)}
          </button>
        </div>
      </div>
      <div class="detail-meta">
        <span>Received</span><span>${formatTimestamp(m.created_time)}</span>
        <span>Duration</span><span>${formatDurationMs(m.duration)}</span>
        <span>Status</span><span id="detail-status-value">${m.heard ? "Heard" : "New"}</span>
      </div>
      <div class="player">
        <button class="player-btn" id="player-btn" type="button" aria-label="Play">&#9654;</button>
        <div class="player-track-col">
          <div class="player-track" id="player-track">
            <div class="player-track-fill" id="player-track-fill"></div>
          </div>
          <div class="player-time-row">
            <span id="player-time-current">0:00</span>
            <div class="player-time-right">
              <span class="playback-mode-label">Play on:</span>
              <div class="playback-mode-toggle">
                <button class="mode-btn active" id="mode-speaker-btn" type="button" title="Play in browser" aria-pressed="true">
                  ${SPEAKER_ICON}
                </button>
                <button class="mode-btn" id="mode-phone-btn" type="button" title="Call my extension to listen" aria-pressed="false">
                  ${PHONE_ICON}
                </button>
              </div>
              <span id="player-time-duration">${formatDurationMs(m.duration)}</span>
            </div>
          </div>
          <div id="phone-call-status" class="phone-call-status"></div>
          <div id="player-error" class="error-text"></div>
        </div>
        <audio id="player-audio" preload="metadata" style="display:none;" src="${BASE}/api/messages/${m.id}/audio"></audio>
      </div>
    </div>

    <div class="detail-section">
      <div class="detail-section-heading">Call path</div>
      <div class="call-path-list" id="call-path-list">
        <div style="font-size:12px; opacity:0.5;">Loading…</div>
      </div>
    </div>

    <div class="detail-section">
      <div class="detail-section-heading">Transcription</div>
      <div id="transcription-body">${renderTranscriptionBody(m)}</div>
    </div>

    <div class="detail-section">
      <div class="detail-section-heading">Reviewed by</div>
      <div class="review-list" id="review-list"></div>
    </div>
  `;

  renderReviewList(m);
  loadReviewCallbacks(m);
  loadCallPath(m);
  wireCustomPlayer(m);
  wireTranscriptWords(m);

  el("share-btn").addEventListener("click", () => openShareModal(m));

  el("callback-btn").addEventListener("click", () => {
    const btn = el("callback-btn");
    const statusEl = el("phone-call-status");
    if (btn.disabled) return;
    btn.disabled = true;
    if (statusEl) statusEl.textContent = "Calling your extension…";
    api(`/api/messages/${m.id}/callback`, { method: "POST" })
      .then((result) => {
        if (statusEl) statusEl.textContent = `Ringing ext. ${result.extension} — answer to be transferred to ${formatPhone(result.caller)}.`;
        // Cooldown, not an immediate re-enable -- mirrors placePhoneCall's
        // PHONE_CALL_COOLDOWN_MS reasoning: the request only confirms the
        // first INVITE went out, the extension is still ringing/transferring
        // well after this resolves.
        setTimeout(() => { btn.disabled = false; }, PHONE_CALL_COOLDOWN_MS);
      })
      .catch((err) => {
        if (statusEl) statusEl.textContent = err.message || "Could not place the call.";
        btn.disabled = false;
      });
  });

  const generateTranscriptionBtn = el("generate-transcription-btn");
  if (generateTranscriptionBtn) {
    generateTranscriptionBtn.addEventListener("click", async () => {
      generateTranscriptionBtn.disabled = true;
      generateTranscriptionBtn.textContent = "Transcribing…";
      try {
        const result = await api(`/api/messages/${m.id}/transcribe`, { method: "POST" });
        m.transcription = result.transcription;
        m.transcription_words = result.words;
        renderDetail(m);
      } catch (err) {
        generateTranscriptionBtn.disabled = false;
        generateTranscriptionBtn.textContent = "Generate transcription";
        showApiErrorModal(err);
      }
    });
  }

  el("toggle-heard-btn").addEventListener("click", async (e) => {
    if (e.ctrlKey || e.metaKey) {
      // Debug/testing shortcut: toggle the reviewed-tracking write path
      // (app_db.mark_reviewed / remove_review) without playing audio or
      // waiting out the dwell timer, and without touching the DB by hand.
      if (reviewedByMe(m)) {
        await unmarkReviewed(m);
      } else {
        await markReviewed(m);
      }
      return;
    }
    const newHeard = !m.heard;
    const result = await api(`/api/messages/${m.id}/heard`, { method: "POST", body: JSON.stringify({ heard: newHeard }) });
    m.heard = result.heard;
    renderMessages();
    renderDetail(m);
    await loadMailboxes();
  });

  el("delete-btn").addEventListener("click", async () => {
    if (!canDelete) return;
    // Hard delete via the native 3CX RPC (threecx_notify.notify_delete) --
    // the row is gone from 3CX outright, not just hidden, so confirm first.
    if (!window.confirm("Delete this voicemail? This cannot be undone.")) return;
    const btn = el("delete-btn");
    btn.disabled = true;
    try {
      await api(`/api/messages/${m.id}`, { method: "DELETE" });
      state.messages = state.messages.filter((msg) => msg.id !== m.id);
      if (state.currentMessageId === m.id) state.currentMessageId = null;
      renderMessages();
      el("detail-panel").innerHTML = `<div class="detail-empty">Select a message to view details.</div>`;
      await loadMailboxes();
    } catch (err) {
      btn.disabled = false;
      showApiErrorModal(err);
    }
  });
}

// --- share modal ---
function downloadAudio(m) {
  const a = document.createElement("a");
  a.href = `${BASE}/api/messages/${m.id}/audio`;
  a.download = `voicemail-${m.id}.wav`;
  document.body.appendChild(a);
  a.click();
  a.remove();
}

function renderShareModalBody(m) {
  const body = el("share-modal-body");
  // m.callee is the mailbox this message lives in -- the link opens straight
  // to it, and the server's normal per-mailbox access check (see
  // loadMessages' 403 handling) is what decides whether whoever it's sent
  // to can actually see it.
  const link = `${window.location.origin}${messagePath(m.callee, m.id)}`;
  body.innerHTML = `
    <div class="field">
      <label class="detail-section-heading">Link</label>
      <div class="share-link-row">
        <input class="input" id="share-link-input" type="text" readonly value="${escapeHtml(link)}">
        <button class="btn btn-secondary" id="share-copy-link-btn" type="button" title="Copy link">Copy</button>
      </div>
      <div class="share-hint" id="share-copy-hint">
        Anyone with this link and access to this mailbox can open it directly.
      </div>
    </div>
    <div class="field">
      <label class="detail-section-heading">Recording</label>
      <button class="btn btn-secondary" id="share-download-btn" type="button">Download</button>
      <div class="share-hint">Saves the WAV locally to attach wherever you need it.</div>
    </div>
  `;

  el("share-download-btn").addEventListener("click", () => {
    downloadAudio(m);
    closeShareModal();
  });
  el("share-copy-link-btn").addEventListener("click", () => copyShareLink(link));
}

// Tries the modern async Clipboard API first, but that's only available in
// a secure context (HTTPS/localhost) -- this app is plain HTTP on the LAN,
// so browsers will refuse it there. Falls back to the old select+execCommand
// trick, which still works over HTTP; if even that fails, the link is left
// selected so the user can just press Ctrl+C themselves.
async function copyShareLink(link) {
  const hint = el("share-copy-hint");
  const input = el("share-link-input");
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(link);
    } else {
      input.select();
      input.setSelectionRange(0, link.length);
      if (!document.execCommand("copy")) throw new Error("execCommand copy failed");
    }
    hint.textContent = "Link copied.";
  } catch {
    input.select();
    hint.textContent = "Couldn't copy automatically — the link is selected, press Ctrl+C.";
  }
}

function openShareModal(m) {
  renderShareModalBody(m);
  el("share-modal-overlay").classList.remove("hidden");
}

function closeShareModal() {
  el("share-modal-overlay").classList.add("hidden");
}

el("share-modal-close").addEventListener("click", closeShareModal);

// --- settings / admin ---

// Shows the settings modal for the given tab/selected-item, or -- for a
// pasted /app/settings/... link from a non-admin -- a Permission Denied
// modal instead, same as an unauthorized mailbox link (see loadMessages).
// Called both by the settings-btn click below and by applyRoute() for
// popstate/initial-load, so it never touches the URL itself.
async function openSettingsOverlay(tab, selected) {
  if (!state.me.is_admin) {
    showApiErrorModal({ status: 403, message: "Admin only" });
    setUrl(mailboxPath(state.currentExtension), { replace: true });
    return;
  }
  el("settings-overlay").classList.remove("hidden");
  await renderSettings(tab, selected);
}

function closeSettingsOverlay() {
  el("settings-overlay").classList.add("hidden");
}

el("settings-btn").addEventListener("click", () => {
  setUrl(SETTINGS_PATH);
  openSettingsOverlay("general", null);
});
el("settings-close").addEventListener("click", () => {
  setUrl(mailboxPath(state.currentExtension));
  closeSettingsOverlay();
});

// Searchable checkbox combobox for picking a set of extensions, with the
// current selection shown as removable chips (matches the sidebar's pill
// styling). The one exception is the department mailbox's implicit-member
// list (see renderMailboxGroupMembers) — that's a fixed checkbox list, not
// a search-driven "add" control, since it always shows every department
// member regardless of search term so exclusions stay visible.
function comboSelect(container, selectedSet, allExtensions, opts = {}) {
  const exclude = opts.exclude || (() => false);
  const placeholder = opts.placeholder || "Search ext or name...";

  container.classList.add("combo-select");
  container.innerHTML = `
    <div class="combo-chips"></div>
    <div class="combo-search-wrap">
      <input class="input combo-search" placeholder="${escapeHtml(placeholder)}" autocomplete="off">
      <div class="combo-dropdown hidden"></div>
    </div>
  `;

  const chipsEl = container.querySelector(".combo-chips");
  const searchInput = container.querySelector(".combo-search");
  const dropdown = container.querySelector(".combo-dropdown");

  function nameFor(ext) {
    const found = allExtensions.find((e) => e.extension === ext);
    return found ? fullName(found.firstname, found.lastname) : ext;
  }

  function drawChips() {
    chipsEl.innerHTML = [...selectedSet]
      .map(
        (ext) => `
        <span class="chip" data-ext="${ext}">
          ${pillName(ext, nameFor(ext))}
          <button type="button" data-ext="${ext}" aria-label="Remove">&times;</button>
        </span>
      `
      )
      .join("");
    chipsEl.querySelectorAll("button").forEach((btn) =>
      btn.addEventListener("click", () => {
        selectedSet.delete(btn.dataset.ext);
        drawChips();
        drawDropdown();
      })
    );
  }

  function drawDropdown() {
    const term = searchInput.value.trim().toLowerCase();
    const options = allExtensions
      .filter((e) => !exclude(e.extension))
      .filter(
        (e) =>
          !term ||
          e.extension.toLowerCase().includes(term) ||
          `${e.firstname} ${e.lastname}`.toLowerCase().includes(term)
      );
    dropdown.innerHTML = options.length
      ? options
          .map(
            (e) => `
        <label class="combo-option">
          <input type="checkbox" data-ext="${e.extension}" ${selectedSet.has(e.extension) ? "checked" : ""}>
          ${pillName(e.extension, fullName(e.firstname, e.lastname))}
        </label>
      `
          )
          .join("")
      : `<div class="combo-empty">No matches</div>`;
    dropdown.querySelectorAll("input[type=checkbox]").forEach((cb) => {
      cb.addEventListener("change", () => {
        if (cb.checked) selectedSet.add(cb.dataset.ext);
        else selectedSet.delete(cb.dataset.ext);
        drawChips();
      });
    });
  }

  searchInput.addEventListener("focus", () => {
    dropdown.classList.remove("hidden");
    drawDropdown();
  });
  searchInput.addEventListener("input", drawDropdown);

  drawChips();
}

// Search-to-add combobox for granting mailbox access: typing filters a
// dropdown of not-yet-granted extensions, clicking one adds it. Grants
// already made are rendered as a list below (not chips) so each row has
// room for its own "Hide review status" checkbox — whether this viewer's
// review activity should stay invisible to the mailbox owner, per grant
// (see access.viewers_for_mailbox's is_hidden_reviewer) — plus an "x" to
// remove the grant entirely.
// `grants` is a Map<extension, {hide_review_status, mwi_suppress, notify_suppress}>.
// Pass opts.mwiSuppress: true to also render a "Suppress MWI" checkbox per
// row, and opts.notifySuppress: true for a "Suppress notifications" one.
// Shown on both the Departments tab (group-mailbox viewers, where it's
// immediately meaningful) and the Users tab (currently a no-op in practice
// since that picker excludes group mailboxes -- see openUserDetail's
// departmentMailboxExts exclusion -- but kept consistent across both
// editors rather than surprising an admin with a missing option).
function grantsSelect(container, grants, allExtensions, opts = {}) {
  const exclude = opts.exclude || (() => false);
  const placeholder = opts.placeholder || "Search ext or name...";
  const showMwiSuppress = !!opts.mwiSuppress;
  const showNotifySuppress = !!opts.notifySuppress;

  container.classList.add("combo-select");
  container.innerHTML = `
    <div class="combo-search-wrap">
      <input class="input combo-search" placeholder="${escapeHtml(placeholder)}" autocomplete="off">
      <div class="combo-dropdown hidden"></div>
    </div>
    <div class="grant-list"></div>
  `;

  const searchInput = container.querySelector(".combo-search");
  const dropdown = container.querySelector(".combo-dropdown");
  const grantList = container.querySelector(".grant-list");

  function nameFor(ext) {
    const found = allExtensions.find((e) => e.extension === ext);
    return found ? fullName(found.firstname, found.lastname) : ext;
  }

  function drawGrantList() {
    const exts = [...grants.keys()];
    grantList.innerHTML = exts.length
      ? exts
          .map((ext) => {
            const status = grants.get(ext) || {};
            return `
        <div class="grant-row" data-ext="${ext}">
          ${pillName(ext, nameFor(ext))}
          <label class="grant-row-hide">
            <input type="checkbox" class="grant-hide-checkbox" data-ext="${ext}" ${status.hide_review_status ? "checked" : ""}>
            Hide review status
          </label>
          ${
            showMwiSuppress
              ? `
          <label class="grant-row-hide">
            <input type="checkbox" class="grant-mwi-suppress-checkbox" data-ext="${ext}" ${status.mwi_suppress ? "checked" : ""}>
            Suppress MWI
          </label>`
              : ""
          }
          ${
            showNotifySuppress
              ? `
          <label class="grant-row-hide">
            <input type="checkbox" class="grant-notify-suppress-checkbox" data-ext="${ext}" ${status.notify_suppress ? "checked" : ""}>
            Suppress notifications
          </label>`
              : ""
          }
          <button type="button" class="grant-remove-btn" data-ext="${ext}" aria-label="Remove">&times;</button>
        </div>
      `;
          })
          .join("")
      : `<div class="combo-empty">No mailboxes granted.</div>`;

    grantList.querySelectorAll(".grant-hide-checkbox").forEach((cb) => {
      cb.addEventListener("change", () => {
        const status = grants.get(cb.dataset.ext) || {};
        grants.set(cb.dataset.ext, { ...status, hide_review_status: cb.checked });
      });
    });
    grantList.querySelectorAll(".grant-mwi-suppress-checkbox").forEach((cb) => {
      cb.addEventListener("change", () => {
        const status = grants.get(cb.dataset.ext) || {};
        grants.set(cb.dataset.ext, { ...status, mwi_suppress: cb.checked });
      });
    });
    grantList.querySelectorAll(".grant-notify-suppress-checkbox").forEach((cb) => {
      cb.addEventListener("change", () => {
        const status = grants.get(cb.dataset.ext) || {};
        grants.set(cb.dataset.ext, { ...status, notify_suppress: cb.checked });
      });
    });
    grantList.querySelectorAll(".grant-remove-btn").forEach((btn) =>
      btn.addEventListener("click", () => {
        grants.delete(btn.dataset.ext);
        drawGrantList();
        drawDropdown();
      })
    );
  }

  function drawDropdown() {
    const term = searchInput.value.trim().toLowerCase();
    const options = allExtensions
      .filter((e) => !exclude(e.extension) && !grants.has(e.extension))
      .filter(
        (e) =>
          !term ||
          e.extension.toLowerCase().includes(term) ||
          `${e.firstname} ${e.lastname}`.toLowerCase().includes(term)
      );
    dropdown.innerHTML = options.length
      ? options
          .map(
            (e) => `
        <div class="combo-option" data-ext="${e.extension}">
          ${pillName(e.extension, fullName(e.firstname, e.lastname))}
        </div>
      `
          )
          .join("")
      : `<div class="combo-empty">No matches</div>`;
    dropdown.querySelectorAll(".combo-option").forEach((opt) => {
      opt.addEventListener("click", () => {
        grants.set(opt.dataset.ext, { hide_review_status: false, mwi_suppress: false, notify_suppress: false });
        searchInput.value = "";
        drawGrantList();
        drawDropdown();
        dropdown.classList.add("hidden");
      });
    });
  }

  searchInput.addEventListener("focus", () => {
    dropdown.classList.remove("hidden");
    drawDropdown();
  });
  searchInput.addEventListener("input", () => {
    dropdown.classList.remove("hidden");
    drawDropdown();
  });

  drawGrantList();
}

// Single delegated listener (rather than one per comboSelect instance) so
// repeatedly opening the Users/Departments detail panes doesn't pile up
// duplicate document-level handlers over a long settings session.
document.addEventListener("click", (e) => {
  document.querySelectorAll(".combo-select").forEach((wrap) => {
    if (!wrap.contains(e.target)) {
      wrap.querySelector(".combo-dropdown")?.classList.add("hidden");
    }
  });
});

// `initialTab`/`initialSelected` reproduce a deep link like
// /app/settings/users/205 -- opening straight to the Users tab with that
// user's detail pane already showing, as if it had just been clicked.
async function renderSettings(initialTab = "general", initialSelected = null) {
  const body = el("settings-body");
  body.innerHTML = `
    <div class="settings-tabs">
      <button class="tab-btn" type="button" data-tab="general">General</button>
      <button class="tab-btn" type="button" data-tab="users">Users</button>
      <button class="tab-btn" type="button" data-tab="departments">Departments</button>
      <button class="tab-btn" type="button" data-tab="transcription">Transcription</button>
      <button class="tab-btn" type="button" data-tab="notifications">Notifications</button>
    </div>

    <div class="tab-panel" id="tab-general">
      <div class="admin-columns">
        <div class="admin-detail-col" style="max-width:480px;">
          <h3 style="margin:0 0 var(--space-2) 0;">Branding</h3>
          <div class="field">
            <label class="detail-section-heading">Company / brand name</label>
            <input class="input" id="general-brand-name-input" placeholder="Your Phone System">
            <div class="share-hint">Shown in notification emails, e.g. "This notification was sent from &lt;name&gt; Voicemail Manager."</div>
          </div>
          <h3 style="margin:var(--space-5) 0 var(--space-2) 0;border-top:1px solid var(--border-color, #d7d3d3);padding-top:var(--space-4);display:flex;align-items:center;gap:8px;">
            3CX Extension
            <span id="phone-reg-status" style="font-size:12px;font-weight:normal;display:inline-flex;align-items:center;gap:5px;"></span>
          </h3>
          <div class="field">
            <label class="detail-section-heading">PBX host</label>
            <input class="input" id="phone-host-input" placeholder="127.0.0.1">
          </div>
          <div class="field" style="margin-top:var(--space-3);">
            <label class="detail-section-heading">PBX domain</label>
            <input class="input" id="phone-domain-input" placeholder="example.3cx.ca">
          </div>
          <div class="field" style="margin-top:var(--space-3);">
            <label class="detail-section-heading">Port</label>
            <input class="input" type="number" id="phone-port-input" placeholder="5060">
          </div>
          <div class="field" style="margin-top:var(--space-3);">
            <label class="detail-section-heading">Transport</label>
            <select class="input" id="phone-transport-select">
              <option value="udp">UDP</option>
              <option value="tcp">TCP</option>
              <option value="tls">TLS</option>
            </select>
          </div>
          <div class="field" style="margin-top:var(--space-3);">
            <label class="detail-section-heading">Extension</label>
            <input class="input" id="phone-extension-input" placeholder="998">
          </div>
          <div class="field" style="margin-top:var(--space-3);">
            <label class="detail-section-heading">Auth ID</label>
            <input class="input" id="phone-auth-id-input">
          </div>
          <div class="field" style="margin-top:var(--space-3);">
            <label class="detail-section-heading">Password</label>
            <input class="input" type="password" id="phone-password-input" autocomplete="new-password">
            <div class="share-hint" id="phone-password-hint"></div>
          </div>
          <div class="share-hint" style="margin-top:var(--space-3);">Changes take effect after the vm-manager service is next restarted.</div>

          <h3 style="margin:var(--space-5) 0 var(--space-2) 0;border-top:1px solid var(--border-color, #d7d3d3);padding-top:var(--space-4);">Sign-in (Microsoft Entra ID)</h3>
          <div class="field">
            <label class="detail-section-heading">Tenant ID</label>
            <input class="input" id="signin-tenant-input">
          </div>
          <div class="field" style="margin-top:var(--space-3);">
            <label class="detail-section-heading">Client ID</label>
            <input class="input" id="signin-client-input">
          </div>
          <div class="field" style="margin-top:var(--space-3);">
            <label class="detail-section-heading">Override emails (comma-separated)</label>
            <input class="input" id="signin-override-input">
            <div class="share-hint">Shared accounts (e.g. several admins) that all sign in as the management extension instead of matching their own mailbox.</div>
          </div>
          <div class="share-hint" style="margin-top:var(--space-3);">Changes take effect after the vm-manager service is next restarted.</div>

          <button class="btn btn-primary" id="general-save-btn" type="button" style="margin-top:var(--space-5);">Save</button>
          <div class="share-hint" id="general-save-status"></div>
        </div>
      </div>
    </div>

    <div class="tab-panel hidden" id="tab-users">
      <div class="admin-columns">
        <div class="admin-list-col">
          <div class="sidebar-search-row">
            <div class="sidebar-search-box">
              <svg class="search-icon" viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
                <circle cx="6.5" cy="6.5" r="4.5" fill="none" stroke="currentColor" stroke-width="1.5"/>
                <line x1="9.8" y1="9.8" x2="14" y2="14" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>
              </svg>
              <input class="input sidebar-search-input" id="users-search" placeholder="Search">
              <button type="button" class="sort-icon-btn" id="users-sort-toggle" aria-label="Toggle sort order">
                <span class="sort-icon-box" id="users-sort-icon">1</span>
              </button>
            </div>
          </div>
          <div class="admin-list" id="user-list"></div>
        </div>
        <div class="admin-detail-col" id="user-detail">
          <div class="detail-empty">Select a user to manage the other mailboxes they can view.</div>
        </div>
      </div>
    </div>

    <div class="tab-panel hidden" id="tab-departments">
      <div class="admin-columns">
        <div class="admin-list-col">
          <div class="sidebar-search-row">
            <div class="sidebar-search-box">
              <svg class="search-icon" viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
                <circle cx="6.5" cy="6.5" r="4.5" fill="none" stroke="currentColor" stroke-width="1.5"/>
                <line x1="9.8" y1="9.8" x2="14" y2="14" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>
              </svg>
              <input class="input sidebar-search-input" id="departments-search" placeholder="Search">
              <button type="button" class="sort-icon-btn" id="departments-sort-toggle" aria-label="Toggle sort order">
                <span class="sort-icon-box" id="departments-sort-icon">A</span>
              </button>
            </div>
          </div>
          <div class="admin-list" id="department-list"></div>
        </div>
        <div class="admin-detail-col" id="department-detail">
          <div class="detail-empty">Select a department to manage its shared mailboxes.</div>
        </div>
      </div>
    </div>

    <div class="tab-panel hidden" id="tab-transcription">
      <div class="admin-columns">
        <div class="admin-detail-col" style="max-width:480px;">
          <label class="checkbox-row">
            <input type="checkbox" id="transcription-enabled-checkbox">
            Automatically transcribe voicemails
          </label>
          <div class="field" style="margin-top:var(--space-3);">
            <label class="detail-section-heading">Engine</label>
            <select class="input" id="transcription-engine-select">
              <option value="local">Local (faster-whisper, runs on this server)</option>
              <option value="openai">OpenAI (sends the recording to OpenAI's API)</option>
            </select>
            <div class="share-hint" id="transcription-openai-hint"></div>
          </div>
          <button class="btn btn-primary" id="transcription-save-btn" type="button" style="margin-top:var(--space-3);">Save</button>
          <div class="share-hint" id="transcription-save-status"></div>

          <h3 style="margin:var(--space-5) 0 var(--space-2) 0;border-top:1px solid var(--border-color, #d7d3d3);padding-top:var(--space-4);">Advanced (engine tuning)</h3>
          <div class="field">
            <label class="detail-section-heading">Local model size</label>
            <select class="input" id="whisper-model-size-select">
              <option value="tiny">tiny (~75MB, fastest)</option>
              <option value="base">base</option>
              <option value="small">small</option>
              <option value="medium">medium</option>
              <option value="large-v3">large-v3 (most accurate, slowest)</option>
            </select>
          </div>
          <div class="field" style="margin-top:var(--space-3);">
            <label class="detail-section-heading">Local compute type</label>
            <select class="input" id="whisper-compute-type-select">
              <option value="int8">int8</option>
              <option value="int8_float16">int8_float16</option>
              <option value="float16">float16</option>
              <option value="float32">float32</option>
            </select>
          </div>
          <div class="field" style="margin-top:var(--space-3);">
            <label class="detail-section-heading">Local CPU threads</label>
            <input class="input" type="number" id="whisper-cpu-threads-input" min="1">
          </div>
          <div class="field" style="margin-top:var(--space-3);">
            <label class="detail-section-heading">Local memory limit (MB)</label>
            <input class="input" type="number" id="whisper-memory-limit-input" min="128">
            <div class="share-hint">Local transcription is killed and reported as failed if it exceeds this. Requires a restart to take effect.</div>
          </div>
          <div class="field" style="margin-top:var(--space-3);">
            <label class="detail-section-heading">OpenAI model</label>
            <input class="input" id="whisper-openai-model-input" placeholder="gpt-4o-transcribe">
            <div class="share-hint">Requires a restart to take effect.</div>
          </div>
          <button class="btn btn-primary" id="whisper-save-btn" type="button" style="margin-top:var(--space-3);">Save</button>
          <div class="share-hint" id="whisper-save-status"></div>
        </div>
      </div>
    </div>

    <div class="tab-panel hidden" id="tab-notifications">
      <div class="admin-columns">
        <div class="admin-detail-col" style="max-width:480px;">
          <label class="checkbox-row">
            <input type="checkbox" id="notify-smtp-checkbox">
            Email extensions when a new voicemail arrives
          </label>
          <div class="share-hint" id="notify-smtp-hint"></div>
          <label class="checkbox-row" style="margin-top:var(--space-3);">
            <input type="checkbox" id="notify-pwa-checkbox">
            Push a browser notification when a new voicemail arrives
          </label>
          <div class="share-hint" id="notify-pwa-hint">
            Staff also need to click the bell icon in the nav bar on each device they want notified on.
          </div>
          <button class="btn btn-primary" id="notify-save-btn" type="button" style="margin-top:var(--space-3);">Save</button>
          <div class="share-hint" id="notify-save-status"></div>

          <h3 style="margin:var(--space-5) 0 var(--space-2) 0;border-top:1px solid var(--border-color, #d7d3d3);padding-top:var(--space-4);">SMTP connection</h3>
          <div class="field">
            <label class="detail-section-heading">Host</label>
            <input class="input" id="smtp-host-input" placeholder="smtp.office365.com">
          </div>
          <div class="field" style="margin-top:var(--space-3);">
            <label class="detail-section-heading">Port</label>
            <input class="input" type="number" id="smtp-port-input" placeholder="587">
          </div>
          <div class="field" style="margin-top:var(--space-3);">
            <label class="detail-section-heading">Username</label>
            <input class="input" id="smtp-username-input">
          </div>
          <div class="field" style="margin-top:var(--space-3);">
            <label class="detail-section-heading">Password</label>
            <input class="input" type="password" id="smtp-password-input" autocomplete="new-password">
            <div class="share-hint" id="smtp-password-hint"></div>
          </div>
          <div class="field" style="margin-top:var(--space-3);">
            <label class="detail-section-heading">From address</label>
            <input class="input" id="smtp-from-input">
          </div>
          <label class="checkbox-row" style="margin-top:var(--space-3);">
            <input type="checkbox" id="smtp-use-tls-checkbox">
            Use TLS
          </label>
          <div class="share-hint" style="margin-top:var(--space-3);">Changes take effect after the vm-manager service is next restarted.</div>
          <button class="btn btn-primary" id="smtp-save-btn" type="button" style="margin-top:var(--space-3);">Save</button>
          <div class="share-hint" id="smtp-save-status"></div>
        </div>
      </div>
    </div>
  `;

  function activateTab(tab) {
    body.querySelectorAll(".tab-btn").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
    body.querySelectorAll(".tab-panel").forEach((p) => p.classList.toggle("hidden", p.id !== `tab-${tab}`));
  }

  body.querySelectorAll(".tab-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      activateTab(btn.dataset.tab);
      // Manually switching tabs (not picking a specific user/department)
      // drops any selected-item deep link from the URL.
      setUrl(SETTINGS_PATH, { replace: true });
    });
  });
  activateTab(initialTab);

  const extensions = await api("/api/admin/extensions");
  const departments = await api("/api/admin/departments");
  await renderGeneralTab();
  await renderUsersTab(extensions, departments);
  await renderDepartmentsTab(extensions, departments);
  await renderTranscriptionTab();
  await renderNotificationsTab();

  if (initialTab === "users" && initialSelected) {
    const item = el("user-list").querySelector(`.admin-list-item[data-ext="${CSS.escape(initialSelected)}"]`);
    if (item) {
      item.classList.add("active");
      await openUserDetail(initialSelected, extensions, departments);
    } else {
      openErrorModal("Not Found", "HTTP 404 — That user doesn't exist.");
    }
  } else if (initialTab === "departments" && initialSelected) {
    const item = el("department-list").querySelector(`.admin-list-item[data-dept="${CSS.escape(initialSelected)}"]`);
    if (item) {
      item.classList.add("active");
      await openDepartmentDetail(initialSelected, departments, extensions);
    } else {
      openErrorModal("Not Found", "HTTP 404 — That department doesn't exist.");
    }
  }
}

// --- General tab: single Save button for Branding + 3CX Extension +
// Sign-in. Each of those three blocks is independently optional (leaving a
// whole block blank is fine -- that feature just stays disabled), but a
// *partially* filled block is never useful, so validation only kicks in
// once the admin has started typing into a block: fill one field in it and
// the rest of that block's required fields glow red until they're filled
// in too (or the admin blanks the field they started, opting back out).
function markFieldValidity(input, missing) {
  input.classList.toggle("input-needs-value", missing);
}

async function renderGeneralTab() {
  const generalSettings = await api("/api/admin/general-settings");
  const phoneSettings = await api("/api/admin/phone-settings");
  const msAuthSettings = await api("/api/admin/ms-auth-settings");

  const brandInput = el("general-brand-name-input");
  brandInput.value = generalSettings.brand_name;

  const hostInput = el("phone-host-input");
  const domainInput = el("phone-domain-input");
  const portInput = el("phone-port-input");
  const transportSelect = el("phone-transport-select");
  const extensionInput = el("phone-extension-input");
  const authIdInput = el("phone-auth-id-input");
  const passwordInput = el("phone-password-input");
  const passwordHint = el("phone-password-hint");

  hostInput.value = phoneSettings.pbx_host;
  domainInput.value = phoneSettings.pbx_domain;
  portInput.value = phoneSettings.pbx_port;
  transportSelect.value = phoneSettings.pbx_transport;
  extensionInput.value = phoneSettings.extension;
  authIdInput.value = phoneSettings.auth_id;
  passwordInput.value = "";
  passwordInput.placeholder = phoneSettings.password_set ? "••••••••" : "";
  let phonePasswordSet = phoneSettings.password_set;
  passwordHint.textContent = phonePasswordSet
    ? "Leave blank to keep the current password."
    : "Not set — phone playback stays unavailable until this is filled in.";

  renderPhoneRegStatus();

  const tenantInput = el("signin-tenant-input");
  const clientInput = el("signin-client-input");
  const overrideInput = el("signin-override-input");

  tenantInput.value = msAuthSettings.tenant_id;
  clientInput.value = msAuthSettings.client_id;
  overrideInput.value = msAuthSettings.override_emails;

  const saveBtn = el("general-save-btn");
  const saveStatus = el("general-save-status");

  // A block only needs validating once the admin has put something into
  // one of its fields -- otherwise "leave this whole feature unconfigured"
  // would itself trip the "incomplete" glow.
  function validate() {
    const phoneFields = [hostInput, domainInput, extensionInput, authIdInput];
    const phoneTouched = phoneFields.some((f) => f.value.trim()) || (passwordInput.value.trim() && !phonePasswordSet);
    const phoneNeedsPassword = !phonePasswordSet && !passwordInput.value.trim();
    let ok = true;
    phoneFields.forEach((f) => {
      const missing = phoneTouched && !f.value.trim();
      markFieldValidity(f, missing);
      if (missing) ok = false;
    });
    const passwordMissing = phoneTouched && phoneNeedsPassword;
    markFieldValidity(passwordInput, passwordMissing);
    if (passwordMissing) ok = false;

    const signinFields = [tenantInput, clientInput];
    const signinTouched = signinFields.some((f) => f.value.trim());
    signinFields.forEach((f) => {
      const missing = signinTouched && !f.value.trim();
      markFieldValidity(f, missing);
      if (missing) ok = false;
    });

    return ok;
  }

  // Clear a field's glow as soon as it's fixed, without waiting for the
  // next Save click.
  [hostInput, domainInput, extensionInput, authIdInput, passwordInput, tenantInput, clientInput].forEach((f) => {
    f.addEventListener("input", validate);
  });

  saveBtn.addEventListener("click", async () => {
    if (!validate()) {
      saveStatus.textContent = "Fill in every highlighted field, or clear the whole section to leave it unconfigured.";
      return;
    }
    saveBtn.disabled = true;
    saveStatus.textContent = "Saving…";
    try {
      const updatedGeneral = await api("/api/admin/general-settings", {
        method: "PUT",
        body: JSON.stringify({ brand_name: brandInput.value }),
      });
      brandInput.value = updatedGeneral.brand_name;

      const updatedPhone = await api("/api/admin/phone-settings", {
        method: "PUT",
        body: JSON.stringify({
          pbx_host: hostInput.value,
          pbx_domain: domainInput.value,
          pbx_port: parseInt(portInput.value, 10) || 5060,
          pbx_transport: transportSelect.value,
          extension: extensionInput.value,
          auth_id: authIdInput.value,
          password: passwordInput.value || null,
        }),
      });
      hostInput.value = updatedPhone.pbx_host;
      domainInput.value = updatedPhone.pbx_domain;
      portInput.value = updatedPhone.pbx_port;
      transportSelect.value = updatedPhone.pbx_transport;
      extensionInput.value = updatedPhone.extension;
      authIdInput.value = updatedPhone.auth_id;
      passwordInput.value = "";
      phonePasswordSet = updatedPhone.password_set;
      passwordInput.placeholder = updatedPhone.password_set ? "••••••••" : "";
      passwordHint.textContent = updatedPhone.password_set
        ? "Leave blank to keep the current password."
        : "Not set — phone playback stays unavailable until this is filled in.";
      renderPhoneRegStatus();

      const updatedSignin = await api("/api/admin/ms-auth-settings", {
        method: "PUT",
        body: JSON.stringify({
          tenant_id: tenantInput.value,
          client_id: clientInput.value,
          override_emails: overrideInput.value,
        }),
      });
      tenantInput.value = updatedSignin.tenant_id;
      clientInput.value = updatedSignin.client_id;
      overrideInput.value = updatedSignin.override_emails;

      validate();
      saveStatus.textContent = "Saved. Restart the vm-manager service for the 3CX Extension and Sign-in changes to take effect.";
    } catch (err) {
      saveStatus.textContent = "";
      showApiErrorModal(err);
    } finally {
      saveBtn.disabled = false;
    }
  });
}

// --- General tab: Phone header registration indicator ---------------------
async function renderPhoneRegStatus() {
  const statusEl = el("phone-reg-status");
  if (!statusEl) return;
  statusEl.innerHTML = `<span class="status-dot" style="background:#9e9e9e;"></span> Checking…`;
  try {
    const status = await api("/api/admin/phone-status");
    let color, label;
    if (!status.configured) {
      color = "#9e9e9e";
      label = "Not configured";
    } else if (status.registered) {
      color = "#2e7d32";
      label = "Registered with 3CX";
    } else {
      color = "#c62828";
      label = status.reason ? `Not registered (${status.reason})` : "Not registered";
    }
    statusEl.innerHTML = `<span class="status-dot" style="background:${color};"></span> ${escapeHtml(label)}`;
  } catch (err) {
    statusEl.innerHTML = "";
  }
}

// --- Transcription tab: enable/disable + engine choice ------------------------
async function renderTranscriptionTab() {
  const settings = await api("/api/admin/transcription-settings");

  const enabledCheckbox = el("transcription-enabled-checkbox");
  const engineSelect = el("transcription-engine-select");
  const openaiHint = el("transcription-openai-hint");
  const saveBtn = el("transcription-save-btn");
  const saveStatus = el("transcription-save-status");

  enabledCheckbox.checked = settings.enabled;
  engineSelect.value = settings.engine;

  const openaiOption = engineSelect.querySelector('option[value="openai"]');
  openaiOption.disabled = !settings.openai_available;
  openaiHint.textContent = settings.openai_available
    ? "Voicemail recordings are sent to OpenAI's API for transcription when this engine is selected."
    : "Unavailable — set OPENAI_API_KEY in this server's .env to enable the OpenAI engine.";

  saveBtn.addEventListener("click", async () => {
    saveBtn.disabled = true;
    saveStatus.textContent = "Saving…";
    try {
      const updated = await api("/api/admin/transcription-settings", {
        method: "PUT",
        body: JSON.stringify({ enabled: enabledCheckbox.checked, engine: engineSelect.value }),
      });
      enabledCheckbox.checked = updated.enabled;
      engineSelect.value = updated.engine;
      saveStatus.textContent = "Saved.";
    } catch (err) {
      saveStatus.textContent = "";
      showApiErrorModal(err);
    } finally {
      saveBtn.disabled = false;
    }
  });

  await renderWhisperSection();
}

// --- Transcription tab: Whisper/OpenAI engine tuning ----------------------
async function renderWhisperSection() {
  const settings = await api("/api/admin/whisper-settings");

  const modelSizeSelect = el("whisper-model-size-select");
  const computeTypeSelect = el("whisper-compute-type-select");
  const cpuThreadsInput = el("whisper-cpu-threads-input");
  const memoryLimitInput = el("whisper-memory-limit-input");
  const openaiModelInput = el("whisper-openai-model-input");
  const saveBtn = el("whisper-save-btn");
  const saveStatus = el("whisper-save-status");

  modelSizeSelect.value = settings.whisper_model_size;
  computeTypeSelect.value = settings.whisper_compute_type;
  cpuThreadsInput.value = settings.whisper_cpu_threads;
  memoryLimitInput.value = settings.whisper_memory_limit_mb;
  openaiModelInput.value = settings.openai_transcribe_model;

  saveBtn.addEventListener("click", async () => {
    saveBtn.disabled = true;
    saveStatus.textContent = "Saving…";
    try {
      const updated = await api("/api/admin/whisper-settings", {
        method: "PUT",
        body: JSON.stringify({
          whisper_model_size: modelSizeSelect.value,
          whisper_compute_type: computeTypeSelect.value,
          whisper_cpu_threads: parseInt(cpuThreadsInput.value, 10) || 2,
          whisper_memory_limit_mb: parseInt(memoryLimitInput.value, 10) || 1024,
          openai_transcribe_model: openaiModelInput.value,
        }),
      });
      modelSizeSelect.value = updated.whisper_model_size;
      computeTypeSelect.value = updated.whisper_compute_type;
      cpuThreadsInput.value = updated.whisper_cpu_threads;
      memoryLimitInput.value = updated.whisper_memory_limit_mb;
      openaiModelInput.value = updated.openai_transcribe_model;
      saveStatus.textContent = "Saved. Model size/compute type/threads apply to the next transcription; memory limit and OpenAI model need a restart.";
    } catch (err) {
      saveStatus.textContent = "";
      showApiErrorModal(err);
    } finally {
      saveBtn.disabled = false;
    }
  });
}

// --- Notifications tab: SMTP / push, enabled independently -------------------
async function renderNotificationsTab() {
  const settings = await api("/api/admin/notification-settings");

  const smtpCheckbox = el("notify-smtp-checkbox");
  const pwaCheckbox = el("notify-pwa-checkbox");
  const smtpHint = el("notify-smtp-hint");
  const pwaHint = el("notify-pwa-hint");
  const saveBtn = el("notify-save-btn");
  const saveStatus = el("notify-save-status");

  smtpCheckbox.checked = settings.smtp_enabled;
  pwaCheckbox.checked = settings.pwa_enabled;
  smtpCheckbox.disabled = !settings.smtp_configured;
  pwaCheckbox.disabled = !settings.pwa_configured;

  smtpHint.textContent = settings.smtp_configured
    ? "Sent to each mailbox's email on file (from 3CX, or overridden per-user in the Users tab)."
    : "Unavailable — fill in the SMTP connection settings below to enable email notifications.";
  pwaHint.textContent = settings.pwa_configured
    ? "Staff also need to click the bell icon in the nav bar on each device they want notified on."
    : "Unavailable — set VAPID_PUBLIC_KEY/VAPID_PRIVATE_KEY/VAPID_CONTACT_EMAIL in this server's .env to enable push.";

  saveBtn.addEventListener("click", async () => {
    saveBtn.disabled = true;
    saveStatus.textContent = "Saving…";
    try {
      const updated = await api("/api/admin/notification-settings", {
        method: "PUT",
        body: JSON.stringify({ smtp_enabled: smtpCheckbox.checked, pwa_enabled: pwaCheckbox.checked }),
      });
      smtpCheckbox.checked = updated.smtp_enabled;
      pwaCheckbox.checked = updated.pwa_enabled;
      saveStatus.textContent = "Saved.";
    } catch (err) {
      saveStatus.textContent = "";
      showApiErrorModal(err);
    } finally {
      saveBtn.disabled = false;
    }
  });

  await renderSmtpSection();
}

// --- Notifications tab: SMTP connection details ---------------------------
async function renderSmtpSection() {
  const settings = await api("/api/admin/smtp-settings");

  const hostInput = el("smtp-host-input");
  const portInput = el("smtp-port-input");
  const usernameInput = el("smtp-username-input");
  const passwordInput = el("smtp-password-input");
  const passwordHint = el("smtp-password-hint");
  const fromInput = el("smtp-from-input");
  const useTlsCheckbox = el("smtp-use-tls-checkbox");
  const saveBtn = el("smtp-save-btn");
  const saveStatus = el("smtp-save-status");

  hostInput.value = settings.smtp_host;
  portInput.value = settings.smtp_port;
  usernameInput.value = settings.smtp_username;
  passwordInput.value = "";
  passwordInput.placeholder = settings.password_set ? "••••••••" : "";
  passwordHint.textContent = settings.password_set
    ? "Leave blank to keep the current password."
    : "Not set — email notifications stay unavailable until this is filled in.";
  fromInput.value = settings.smtp_from;
  useTlsCheckbox.checked = settings.smtp_use_tls;

  saveBtn.addEventListener("click", async () => {
    saveBtn.disabled = true;
    saveStatus.textContent = "Saving…";
    try {
      const updated = await api("/api/admin/smtp-settings", {
        method: "PUT",
        body: JSON.stringify({
          smtp_host: hostInput.value,
          smtp_port: parseInt(portInput.value, 10) || 587,
          smtp_username: usernameInput.value,
          smtp_from: fromInput.value,
          smtp_use_tls: useTlsCheckbox.checked,
          smtp_password: passwordInput.value || null,
        }),
      });
      hostInput.value = updated.smtp_host;
      portInput.value = updated.smtp_port;
      usernameInput.value = updated.smtp_username;
      passwordInput.value = "";
      passwordInput.placeholder = updated.password_set ? "••••••••" : "";
      passwordHint.textContent = updated.password_set
        ? "Leave blank to keep the current password."
        : "Not set — email notifications stay unavailable until this is filled in.";
      fromInput.value = updated.smtp_from;
      useTlsCheckbox.checked = updated.smtp_use_tls;
      saveStatus.textContent = "Saved. Restart the vm-manager service for this to take effect.";
    } catch (err) {
      saveStatus.textContent = "";
      showApiErrorModal(err);
    } finally {
      saveBtn.disabled = false;
    }
  });
}

// --- Users tab: pick a user, grant them visibility into other mailboxes ------
async function renderUsersTab(extensions, departments) {
  // Department mailboxes (Departments tab) aren't individual staff — their
  // access is managed there (implicit dept access + extra reviewers), so
  // they shouldn't also show up as a person to configure in the Users list.
  const departmentMailboxExts = new Set(departments.flatMap((d) => d.mailboxes));
  const userExtensions = extensions.filter((e) => !departmentMailboxExts.has(e.extension));

  const searchInput = el("users-search");
  const sortToggle = el("users-sort-toggle");

  function updateSortToggle() {
    const alpha = state.usersSort === "alpha";
    el("users-sort-icon").textContent = alpha ? "A" : "1";
    sortToggle.title = alpha ? "Sorted A–Z — click to sort by extension" : "Sorted by extension — click to sort A–Z";
  }

  function compareUsers(a, b) {
    if (state.usersSort === "alpha") {
      const nameA = fullName(a.firstname, a.lastname).toLowerCase();
      const nameB = fullName(b.firstname, b.lastname).toLowerCase();
      if (nameA !== nameB) return nameA < nameB ? -1 : 1;
      return 0;
    }
    return parseInt(a.extension, 10) - parseInt(b.extension, 10);
  }

  function renderList() {
    const term = searchInput.value.trim().toLowerCase();
    const filtered = userExtensions.filter((e) => {
      if (!term) return true;
      const name = fullName(e.firstname, e.lastname).toLowerCase();
      return (
        e.extension.toLowerCase().includes(term) ||
        name.includes(term) ||
        (e.department || "").toLowerCase().includes(term) ||
        (e.email || "").toLowerCase().includes(term)
      );
    });
    filtered.sort(compareUsers);

    const list = el("user-list");
    list.innerHTML = filtered
      .map(
        (e) => `
        <div class="admin-list-item" data-ext="${e.extension}">
          ${pillName(e.extension, fullName(e.firstname, e.lastname))}
          ${e.email ? `<div class="sub">${escapeHtml(e.email)}</div>` : ""}
          ${e.department ? `<div class="sub">${escapeHtml(e.department)}</div>` : ""}
          ${e.hidden ? `<div class="sub">Hidden</div>` : ""}
        </div>
      `
      )
      .join("");

    list.querySelectorAll(".admin-list-item").forEach((item) => {
      item.addEventListener("click", async () => {
        list.querySelectorAll(".admin-list-item").forEach((i) => i.classList.toggle("active", i === item));
        setUrl(`${SETTINGS_PATH}/users/${encodeURIComponent(item.dataset.ext)}`);
        await openUserDetail(item.dataset.ext, extensions, departments);
      });
    });
  }

  // renderUsersTab gets re-invoked as a refresh (e.g. after toggling a
  // user's "Hidden" checkbox) without the tab panel's HTML being rebuilt,
  // so guard against re-wiring the same search/sort controls twice.
  if (!searchInput.dataset.wired) {
    searchInput.dataset.wired = "1";
    searchInput.addEventListener("input", renderList);
    sortToggle.addEventListener("click", () => {
      state.usersSort = state.usersSort === "ext" ? "alpha" : "ext";
      updateSortToggle();
      renderList();
    });
  }

  updateSortToggle();
  renderList();
}

async function openUserDetail(extension, extensions, departments) {
  const user = extensions.find((e) => e.extension === extension);
  const { mailboxes } = await api(`/api/admin/users/${encodeURIComponent(extension)}/grants`);
  const grants = new Map(
    mailboxes.map((g) => [
      g.mailbox_extension,
      { hide_review_status: g.hide_review_status, mwi_suppress: g.mwi_suppress, notify_suppress: g.notify_suppress },
    ])
  );

  // Department mailboxes already grant access to the whole department (and
  // take outside reviewers via the Departments tab), so offering them here
  // too would just be a second, easy-to-forget-about path to the same
  // access — leave them off the "granted" picker's option list entirely.
  const departmentMailboxExts = new Set(departments.flatMap((d) => d.mailboxes));

  const detail = el("user-detail");
  detail.innerHTML = `
    <h4 style="margin-top:0;">${pillName(extension, fullName(user.firstname, user.lastname))}</h4>
    <p style="font-size:12px; opacity:0.6;">
      Grant this user visibility into other mailboxes, on top of their own and any Supervisor/Manager/System Owner access.
    </p>
    <div class="field">
      <label>Granted mailboxes</label>
      <div id="user-mailbox-select"></div>
    </div>
    <div class="field" style="margin-top:var(--space-3);">
      <label style="display:flex; align-items:center; gap:6px; font-weight:normal;">
        <input type="checkbox" id="user-hidden-checkbox" ${user.hidden ? "checked" : ""}>
        Hide extension
      </label>
    </div>
    <button class="btn btn-primary" id="user-grants-save-btn" style="width:auto; margin-top:var(--space-3);">Save</button>
  `;

  grantsSelect(el("user-mailbox-select"), grants, extensions, {
    exclude: (ext) => ext === extension || departmentMailboxExts.has(ext),
    mwiSuppress: true,
    notifySuppress: true,
  });

  el("user-grants-save-btn").addEventListener("click", async () => {
    await api(`/api/admin/users/${encodeURIComponent(extension)}/grants`, {
      method: "PUT",
      body: JSON.stringify({
        mailboxes: [...grants.entries()].map(([mailbox_extension, status]) => ({
          mailbox_extension,
          hide_review_status: status.hide_review_status,
          mwi_suppress: status.mwi_suppress,
          notify_suppress: status.notify_suppress,
        })),
      }),
    });
    await loadMailboxes();
  });

  el("user-hidden-checkbox").addEventListener("change", async (e) => {
    const hidden = e.target.checked;
    await api(`/api/admin/users/${encodeURIComponent(extension)}/hidden`, {
      method: "PUT",
      body: JSON.stringify({ hidden }),
    });
    user.hidden = hidden;
    await renderUsersTab(extensions, departments);
    await loadMailboxes();
  });
}

// --- Departments tab: designate department mailboxes + manage their members --
async function renderDepartmentsTab(extensions, departments) {
  const searchInput = el("departments-search");
  const sortToggle = el("departments-sort-toggle");

  function updateSortToggle() {
    const alpha = state.departmentsSort === "alpha";
    el("departments-sort-icon").textContent = alpha ? "A" : "1";
    sortToggle.title = alpha ? "Sorted A–Z — click to sort by size" : "Sorted by size — click to sort A–Z";
  }

  function compareDepartments(a, b) {
    if (state.departmentsSort === "alpha") {
      const nameA = a.department.toLowerCase();
      const nameB = b.department.toLowerCase();
      if (nameA !== nameB) return nameA < nameB ? -1 : 1;
      return 0;
    }
    return b.extensions.length - a.extensions.length;
  }

  function renderList() {
    const term = searchInput.value.trim().toLowerCase();
    const filtered = departments.filter((d) => !term || d.department.toLowerCase().includes(term));
    filtered.sort(compareDepartments);

    const list = el("department-list");
    list.innerHTML = filtered
      .map(
        (d) => `
        <div class="admin-list-item" data-dept="${escapeHtml(d.department)}">
          ${escapeHtml(d.department)}
          <div class="sub">${d.extensions.length} extension(s) &middot; ${d.mailboxes.length} mailbox(es)</div>
        </div>
      `
      )
      .join("");

    list.querySelectorAll(".admin-list-item").forEach((item) => {
      item.addEventListener("click", async () => {
        list.querySelectorAll(".admin-list-item").forEach((i) => i.classList.toggle("active", i === item));
        setUrl(`${SETTINGS_PATH}/departments/${encodeURIComponent(item.dataset.dept)}`);
        await openDepartmentDetail(item.dataset.dept, departments, extensions);
      });
    });
  }

  if (!searchInput.dataset.wired) {
    searchInput.dataset.wired = "1";
    searchInput.addEventListener("input", renderList);
    sortToggle.addEventListener("click", () => {
      state.departmentsSort = state.departmentsSort === "alpha" ? "size" : "alpha";
      updateSortToggle();
      renderList();
    });
  }

  updateSortToggle();
  renderList();
}

async function openDepartmentDetail(department, departments, extensions) {
  const dept = departments.find((d) => d.department === department);
  const mailboxSet = new Set(dept.mailboxes);
  const detail = el("department-detail");
  // One save covers the mailbox-list picker plus every mailbox group's
  // member/hide/suppress state below it -- populated by each
  // renderMailboxGroupMembers call, read by the single bottom Save button.
  const pendingMailboxSaves = new Map(); // mailboxExt -> () => Promise

  function renderShell() {
    detail.innerHTML = `
      <h4 style="margin-top:0;">${escapeHtml(department)}</h4>
      <p style="font-size:12px; opacity:0.6;">
        Designate which extensions act as this department's shared voicemail boxes. Everyone in the
        department gets implicit access; you can remove individuals or add outside reviewers below.
      </p>
      <div class="field">
        <label>Department mailboxes</label>
        <div id="dept-mailbox-select"></div>
      </div>
      <div id="dept-mailbox-groups"></div>
      <button class="btn btn-primary" id="dept-save-btn" style="width:auto; margin-top:var(--space-3);">Save</button>
    `;

    comboSelect(el("dept-mailbox-select"), mailboxSet, extensions);

    el("dept-save-btn").addEventListener("click", async () => {
      await api(`/api/admin/departments/${encodeURIComponent(department)}/mailboxes`, {
        method: "PUT",
        body: JSON.stringify({ mailboxes: [...mailboxSet] }),
      });
      dept.mailboxes = [...mailboxSet];
      for (const save of pendingMailboxSaves.values()) {
        await save();
      }
      // Newly-designated mailboxes just got auto-hidden server-side (Users
      // tab checkbox) — refresh the shared extensions list in place so that
      // state, and the Users list's group-mailbox exclusion, stay current.
      const freshExtensions = await api("/api/admin/extensions");
      extensions.splice(0, extensions.length, ...freshExtensions);
      await renderUsersTab(extensions, departments);
      await loadMailboxes();
      await renderMailboxGroups();
    });

    renderMailboxGroups();
  }

  async function renderMailboxGroups() {
    const container = el("dept-mailbox-groups");
    pendingMailboxSaves.clear();
    if (mailboxSet.size === 0) {
      container.innerHTML = "";
      return;
    }
    container.innerHTML = [...mailboxSet]
      .map((ext) => `<div class="mailbox-group" id="dept-mailbox-group-${ext}"></div>`)
      .join("");
    for (const ext of mailboxSet) {
      await renderMailboxGroupMembers(ext);
    }
  }

  async function renderMailboxGroupMembers(mailboxExt) {
    const members = await api(
      `/api/admin/departments/${encodeURIComponent(department)}/mailboxes/${encodeURIComponent(mailboxExt)}/members`
    );
    const box = extensions.find((e) => e.extension === mailboxExt);
    const container = el(`dept-mailbox-group-${mailboxExt}`);
    if (!container) return;

    const excludedSet = new Set(members.excluded);
    const hideReviewSet = new Set(members.implicit.filter((m) => m.hide_review_status).map((m) => m.extension));
    const mwiSuppressSet = new Set(members.implicit.filter((m) => m.mwi_suppress).map((m) => m.extension));
    const notifySuppressSet = new Set(members.implicit.filter((m) => m.notify_suppress).map((m) => m.extension));
    const allImplicit = [...members.implicit.map((m) => m.extension), ...members.excluded];
    const extraGrants = new Map(
      members.extra.map((m) => [
        m.extension,
        { hide_review_status: m.hide_review_status, mwi_suppress: m.mwi_suppress, notify_suppress: m.notify_suppress },
      ])
    );

    function nameFor(ext) {
      const e = extensions.find((x) => x.extension === ext);
      return e ? fullName(e.firstname, e.lastname) : ext;
    }

    container.innerHTML = `
      <h5>${pillName(mailboxExt, box ? fullName(box.firstname, box.lastname) : mailboxExt)}</h5>
      <div style="font-size:11px; text-transform:uppercase; opacity:0.5; margin-bottom:4px;">Department members</div>
      <div id="implicit-members-${mailboxExt}"></div>
      <div class="field">
        <label>Additional viewers</label>
        <div id="extra-select-${mailboxExt}"></div>
      </div>
    `;

    const implicitContainer = el(`implicit-members-${mailboxExt}`);
    if (allImplicit.length === 0) {
      implicitContainer.innerHTML = `<div style="font-size:12px; opacity:0.5;">No other extensions in this department.</div>`;
    } else {
      implicitContainer.innerHTML = allImplicit
        .map(
          (ext) => `
          <div class="grant-row" data-ext="${ext}">
            <label class="member-row">
              <input type="checkbox" class="member-include-checkbox" data-ext="${ext}" ${excludedSet.has(ext) ? "" : "checked"}>
              ${pillName(ext, nameFor(ext))}
            </label>
            <label class="grant-row-hide">
              <input type="checkbox" class="member-hide-checkbox" data-ext="${ext}" ${hideReviewSet.has(ext) ? "checked" : ""}>
              Hide review status
            </label>
            <label class="grant-row-hide">
              <input type="checkbox" class="member-mwi-suppress-checkbox" data-ext="${ext}" ${mwiSuppressSet.has(ext) ? "checked" : ""}>
              Suppress MWI
            </label>
            <label class="grant-row-hide">
              <input type="checkbox" class="member-notify-suppress-checkbox" data-ext="${ext}" ${notifySuppressSet.has(ext) ? "checked" : ""}>
              Suppress notifications
            </label>
          </div>
        `
        )
        .join("");
      implicitContainer.querySelectorAll(".member-include-checkbox").forEach((cb) => {
        cb.addEventListener("change", () => {
          if (cb.checked) excludedSet.delete(cb.dataset.ext);
          else excludedSet.add(cb.dataset.ext);
        });
      });
      implicitContainer.querySelectorAll(".member-hide-checkbox").forEach((cb) => {
        cb.addEventListener("change", () => {
          if (cb.checked) hideReviewSet.add(cb.dataset.ext);
          else hideReviewSet.delete(cb.dataset.ext);
        });
      });
      implicitContainer.querySelectorAll(".member-mwi-suppress-checkbox").forEach((cb) => {
        cb.addEventListener("change", () => {
          if (cb.checked) mwiSuppressSet.add(cb.dataset.ext);
          else mwiSuppressSet.delete(cb.dataset.ext);
        });
      });
      implicitContainer.querySelectorAll(".member-notify-suppress-checkbox").forEach((cb) => {
        cb.addEventListener("change", () => {
          if (cb.checked) notifySuppressSet.add(cb.dataset.ext);
          else notifySuppressSet.delete(cb.dataset.ext);
        });
      });
    }

    grantsSelect(el(`extra-select-${mailboxExt}`), extraGrants, extensions, {
      exclude: (ext) => ext === mailboxExt || allImplicit.includes(ext),
      placeholder: "Search staff outside this department...",
      mwiSuppress: true,
      notifySuppress: true,
    });

    // Registered rather than bound to a per-group button -- the single
    // Save button at the bottom of the whole department detail panel
    // (see renderShell) runs every pending mailbox group's save in turn.
    pendingMailboxSaves.set(mailboxExt, async () => {
      await api(
        `/api/admin/departments/${encodeURIComponent(department)}/mailboxes/${encodeURIComponent(mailboxExt)}/members`,
        {
          method: "PUT",
          body: JSON.stringify({
            excluded: [...excludedSet],
            hidden_review_members: [...hideReviewSet],
            mwi_suppressed_members: [...mwiSuppressSet],
            notify_suppressed_members: [...notifySuppressSet],
            extra: [...extraGrants.entries()].map(([extension, status]) => ({
              extension,
              hide_review_status: status.hide_review_status,
              mwi_suppress: status.mwi_suppress,
              notify_suppress: status.notify_suppress,
            })),
          }),
        }
      );
      await loadMailboxes();
    });
  }

  renderShell();
}

// --- helpers ---
// Mirrors app/email_template.py's format_phone(): a 10-digit NANP number, or
// an 11-digit one with a leading "1" country code, becomes "1 (xxx) xxx-xxxx".
// Anything else (extensions, international numbers, names) passes through
// unchanged.
function formatPhone(raw) {
  const str = (raw === null || raw === undefined) ? "" : String(raw);
  let digits = str.replace(/\D/g, "");
  if (digits.length === 11 && digits.startsWith("1")) {
    digits = digits.slice(1);
  } else if (digits.length !== 10) {
    return str;
  }
  return `1 (${digits.slice(0, 3)}) ${digits.slice(3, 6)}-${digits.slice(6, 10)}`;
}

function escapeHtml(str) {
  if (str === null || str === undefined) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

// 3CX stores s_voicemail.duration in MILLISECONDS (confirmed live: a message
// showing 2 seconds of real audio had duration=2800). The live <audio>
// element's own .duration (used for the player's running time labels) is in
// seconds per the HTML5 spec and does NOT go through this function.
function formatDurationMs(ms) {
  const s = (Number(ms) || 0) / 1000;
  const m = Math.floor(s / 60);
  const rem = Math.round(s % 60);
  return `${m}:${String(rem).padStart(2, "0")}`;
}

// Renders a Date in the viewer's local timezone, replacing the date portion
// with "Today"/"Yesterday" when it falls on one of those calendar days
// (compared in local time, since that's how the viewer reads "today").
function formatLocalDate(d) {
  const now = new Date();
  const startOfDay = (dt) => new Date(dt.getFullYear(), dt.getMonth(), dt.getDate()).getTime();
  const dayDiff = Math.round((startOfDay(now) - startOfDay(d)) / 86400000);

  const time = d.toLocaleString(undefined, { hour: "numeric", minute: "2-digit" });
  if (dayDiff === 0) return `Today, ${time}`;
  if (dayDiff === 1) return `Yesterday, ${time}`;
  return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}

// 3CX's packed timestamps (created_time, heard_time) and this app's own ISO
// timestamps (reviewed_at, via app_db.now_iso()) are both UTC wall-clock
// values with no timezone marker. Parse them as UTC explicitly, then let
// the browser's default locale formatting convert to the viewer's local
// timezone for display (confirmed live: our own heard_time write matches
// 3CX's created_time convention as of the Q5 fix).
function parseServerTimestamp(raw) {
  if (!raw) return null;
  const s = String(raw);

  const packed = s.match(/^(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})/);
  if (packed) {
    const [, year, month, day, hour, minute, second] = packed;
    return new Date(Date.UTC(Number(year), Number(month) - 1, Number(day), Number(hour), Number(minute), Number(second)));
  }

  const iso = s.match(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}/);
  if (iso) return new Date(`${s}Z`);

  return null;
}

function formatTimestamp(raw) {
  const d = parseServerTimestamp(raw);
  return d ? formatLocalDate(d) : raw ? String(raw) : "";
}

// Call-log timestamps come from routes/calls.py as real ISO8601 strings with
// an explicit offset (e.g. "...+00:00", from Postgres timestamptz/psycopg2 +
// FastAPI's jsonable_encoder) -- unlike parseServerTimestamp's inputs
// (3CX's own offset-less packed/ISO timestamps), so this must NOT append
// "Z" itself (that would produce an invalid "...+00:00Z" string). The
// browser's Date constructor already understands an offset-bearing ISO
// string natively.
function parseCallTimestamp(raw) {
  if (!raw) return null;
  const d = new Date(raw);
  return Number.isNaN(d.getTime()) ? null : d;
}

function formatCallTimestamp(raw) {
  const d = parseCallTimestamp(raw);
  return d ? formatLocalDate(d) : raw ? String(raw) : "";
}

// --- live updates (SSE) ---
// Multiple people can have this app open against the same mailboxes at
// once; without this, listening to/reviewing a voicemail on one screen
// wouldn't show up (counter, list, reviewed/heard status) on anyone else's
// until they navigated away and back. One EventSource per tab, opened after
// login -- the browser retries the connection on its own when it drops
// (default EventSource behavior), so there's no manual reconnect logic here.
function connectEvents() {
  const source = new EventSource(`${BASE}/api/events`);
  ["new_message", "heard", "reviewed", "deleted"].forEach((type) => {
    source.addEventListener(type, (e) => handleServerEvent(JSON.parse(e.data)));
  });
}

async function handleServerEvent(event) {
  // Sidebar unread badges can change from any mailbox this session can see,
  // regardless of which one is currently open.
  try {
    state.mailboxes = await api("/api/mailboxes");
  } catch {
    return; // e.g. session expired mid-stream -- api() already redirected
  }
  renderSidebarUser();
  renderMailboxes();

  const showingAffectedMailbox = state.currentExtension === ALL_MAILBOXES || state.currentExtension === event.mailbox;
  if (state.viewMode !== "voicemail" || !showingAffectedMailbox) return;

  if (event.type === "deleted") {
    state.messages = state.messages.filter((m) => m.id !== event.message_id);
    if (state.currentMessageId === event.message_id) {
      state.currentMessageId = null;
      el("detail-panel").innerHTML = `<div class="detail-empty">Select a message to view details.</div>`;
    }
    renderMessages();
    return;
  }

  await refreshCurrentMessages(event);
}

// Re-fetches the current view's message list without resetting pagination
// the way loadMessages() (an explicit navigation) does, then patches the
// open detail panel in place if it's the message that changed -- via
// updateHeardIcon/renderReviewList, never a full renderDetail(), which
// would tear down and restart the audio element mid-playback for whoever
// has it open.
async function refreshCurrentMessages(event) {
  let messages;
  try {
    messages =
      state.currentExtension === ALL_MAILBOXES
        ? await api("/api/mailboxes/all/messages")
        : await api(`/api/mailboxes/${encodeURIComponent(state.currentExtension)}/messages`);
  } catch {
    return;
  }
  state.messages = messages;
  renderMessages();

  if (state.currentMessageId == null) return;
  const updated = messages.find((m) => m.id === state.currentMessageId);
  if (!updated) return;
  updateHeardIcon(updated);
  renderReviewList(updated);
  if (event.type === "reviewed") loadReviewCallbacks(updated);
}

// --- init ---
boot();
