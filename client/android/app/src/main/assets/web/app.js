/* KiteChat client app — OneBot V11 based AI chat.
 * Connection: config.bin (obfuscated, injected by server one-click export)
 * with fallback to same-origin /ws for browser usage.
 * Server address is never stored in plaintext inside the client.
 */
'use strict';

// ============================================================ config
const Config = {
  ws: '',
  http: '',
  appName: 'KiteChat',
};

// XOR + base64 deobfuscation (must match server/exporter.py obfuscate()).
const _OBF_KEY = 'n0v4ch4t$cfg';
function deobfuscate(b64) {
  try {
    const raw = atob(b64.trim());
    let out = '';
    for (let i = 0; i < raw.length; i++) {
      out += String.fromCharCode(raw.charCodeAt(i) ^ _OBF_KEY.charCodeAt(i % _OBF_KEY.length));
    }
    // bytes were latin1-encoded from utf-8; decode properly
    const bytes = Uint8Array.from(out, c => c.charCodeAt(0));
    return new TextDecoder('utf-8').decode(bytes);
  } catch (e) { return ''; }
}

async function loadConfig() {
  // 1) obfuscated config.bin (desktop exe / android apk / deployed)
  //    try fetch first, then XHR (WebView file:// pages often fail fetch),
  //    then a hash-embedded copy injected by the Android shell
  let loaded = false;
  try {
    const r = await fetch('config.bin', { cache: 'no-cache' });
    if (r.ok) { applyConfigText(await r.text()); loaded = true; }
  } catch (e) { /* ignore */ }
  if (!loaded) {
    try {
      const text = await new Promise((resolve, reject) => {
        const x = new XMLHttpRequest();
        x.open('GET', 'config.bin', true);
        x.onload = () => x.status === 0 || x.status === 200 ? resolve(x.responseText) : reject(new Error(x.status));
        x.onerror = () => reject(new Error('xhr error'));
        x.send();
      });
      applyConfigText(text); loaded = true;
    } catch (e) { /* ignore */ }
  }
  if (!loaded && location.hash.indexOf('#cfg=') === 0) {
    try { applyConfigText(decodeURIComponent(location.hash.slice(5))); loaded = true; } catch (e) { /* ignore */ }
  }
  // 2) fallback: derive from page origin (opened server's own page)
  if (!Config.ws) {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    Config.ws = `${proto}://${location.host}/ws`;
  }
  if (!Config.http) {
    try {
      const u = new URL(Config.ws.replace(/^ws/, 'http'));
      u.pathname = '';
      Config.http = u.origin;
    } catch (e) { Config.http = location.origin; }
  }
  // 3) secure-context upgrade: when the page itself is served over https
  //    (the TLS twin port), the ws:// and http:// targets baked into
  //    config.bin would be blocked as mixed content — upgrade both to the
  //    page's own origin (the TLS port serves the same app and WS route).
  if (location.protocol === 'https:') {
    Config.ws = `wss://${location.host}/ws`;
    Config.http = `https://${location.host}`;
  }
}

function applyConfigText(text) {
  try {
    const c = JSON.parse(deobfuscate(text));
    if (c.ws_address) Config.ws = c.ws_address;
    if (c.server_url) Config.http = c.server_url;
    if (c.app_name) Config.appName = c.app_name;
    if (c.version) Config.embeddedVersion = c.version;
  } catch (e) { /* malformed config */ }
}

// server-pushed client settings (reconnect interval etc.), public endpoint
async function loadClientSettings() {
  try {
    const r = await fetch(Config.http + '/api/client-settings', { cache: 'no-cache' });
    if (!r.ok) return;
    const d = (await r.json()).data || {};
    if (d.reconnect_interval) S.reconnectInterval = d.reconnect_interval;
    if (d.app_name) {
      Config.appName = d.app_name;
      document.title = d.app_name;
      const el = $('appNameSide'); if (el) el.textContent = d.app_name;
    }
  } catch (e) { /* offline — keep defaults */ }
}

// ============================================================ state
const S = {
  token: localStorage.getItem('nova_token') || '',
  me: null,
  ws: null,
  connected: false,
  connAttempted: false,   // true once connect() has actually run
  wsStartedAt: 0,         // for CONNECTING-timeout detection
  sessions: [],          // [{id,kind,name,peer,last_msg_ts,last_msg_preview}]
  activeSession: null,
  messages: {},          // session_id -> [msg]
  friends: [],
  requests: [],
  reqId: 0,
  pending: {},           // req_id -> resolve
  typing: {},            // session_id -> bool
  botTyping: {},         // session_id -> bool
  bridgeConnected: true, // AstrBot (OneBot app) bridge state
  bridgeBannerDismissed: false,  // user swiped the bridge banner away
  serverBannerDismissed: false,  // user swiped the server banner away
  reconnectInterval: 5,  // seconds between auto reconnects (admin-configurable)
  reconnectTimer: null,
  nextRetryIn: 0,
  lastDay: '',
};

// ============================================================ utils
const $ = (id) => document.getElementById(id);
function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
function toast(msg, ok = true) {
  const el = document.createElement('div');
  el.className = 'ctoast' + (ok ? '' : ' err');
  el.textContent = msg;
  $('ctoast').appendChild(el);
  setTimeout(() => el.remove(), 3200);
}
function fmtTime(ts) {
  const d = new Date(ts * 1000);
  const now = new Date();
  const hm = d.toTimeString().slice(0, 5);
  if (d.toDateString() === now.toDateString()) return hm;
  if (d.getFullYear() === now.getFullYear())
    return `${d.getMonth() + 1}/${d.getDate()} ${hm}`;
  return `${d.getFullYear()}/${d.getMonth() + 1}/${d.getDate()}`;
}
function fmtClock(ts) { return new Date(ts * 1000).toTimeString().slice(0, 5); }
function dayKey(ts) { return new Date(ts * 1000).toDateString(); }
function avatarColorOf(userId) {
  if (userId === 0) return '#0bb987';
  if (S.me && userId === S.me.user_id) return S.me.avatar_color;
  const f = S.friends.find(f => f.user_id === userId);
  return f ? f.avatar_color : '#8d8aa8';
}
function nameOf(userId) {
  if (userId === 0) return 'Kite AI';
  if (S.me && userId === S.me.user_id) return S.me.nickname;
  const f = S.friends.find(f => f.user_id === userId);
  return f ? f.nickname : '用户' + userId;
}
function isMobile() { return window.innerWidth <= 760; }
function closeSidebarMobile() { if (isMobile()) { $('sidebar').classList.remove('open'); $('sbBackdrop').classList.remove('show'); } }
function openSidebarMobile() { if (isMobile()) { $('sidebar').classList.add('open'); $('sbBackdrop').classList.add('show'); } }

async function http(path, body = null) {
  const opt = { headers: {} };
  if (body) { opt.method = 'POST'; opt.headers['Content-Type'] = 'application/json'; opt.body = JSON.stringify(body); }
  const r = await fetch(Config.http + path, opt);
  const j = await r.json().catch(() => ({ status: 'failed', msg: '网络错误' }));
  if (j.status !== 'ok') throw new Error(j.msg || ('HTTP ' + r.status));
  return j.data;
}

// ============================================================ auth UI
function showLogin() {
  $('loginForm').style.display = 'block';
  $('regForm').style.display = 'none';
  $('authSub').textContent = '私有化 AI 聊天 · 登录你的账号';
}
function showReg() {
  $('loginForm').style.display = 'none';
  $('regForm').style.display = 'block';
  $('authSub').textContent = '注册新账号 · 需要邮箱验证码';
}

async function doLogin() {
  const btn = $('lBtn'); btn.disabled = true; $('lErr').textContent = '';
  try {
    const d = await http('/api/login', {
      username: $('lUser').value.trim(), password: $('lPass').value,
    });
    S.token = d.token; S.me = d.user;
    localStorage.setItem('nova_token', d.token);
    saveAccount(d.user, d.token);
    localStorage.removeItem('kc_acct_add');
    enterApp();
  } catch (e) { $('lErr').textContent = e.message; }
  finally { btn.disabled = false; }
}

let codeCooldown = 0;
async function sendCode() {
  const btn = $('rCodeBtn');
  if (codeCooldown > 0) return;
  const email = $('rEmail').value.trim();
  if (!email) { $('rErr').textContent = '请先填写邮箱'; return; }
  btn.disabled = true; $('rErr').textContent = '';
  btn.innerHTML = '<span class="btn-spin"></span>';
  try {
    await http('/api/register/send-code', { email, purpose: 'register' });
    toast('验证码发送成功，请查收邮箱');
    // bounce watcher polls the mailbox; ask the server for a while whether
    // a "来自qq.com的退信" arrived for this address (= wrong email)
    pollBounce(email);
    codeCooldown = 60;
    const tick = () => {
      if (codeCooldown <= 0) { btn.textContent = '获取验证码'; btn.disabled = false; return; }
      btn.textContent = `${codeCooldown}s 后重发`;
      codeCooldown--; setTimeout(tick, 1000);
    };
    tick();
  } catch (e) {
    $('rErr').textContent = e.message;
    btn.textContent = '获取验证码';
    btn.disabled = false;
  }
}

function pollBounce(email) {
  const deadline = Date.now() + 20000;   // 20s window (QQ bounces take ~5-30s)
  let tries = 0;
  const tick = async () => {
    if (Date.now() > deadline || tries >= 6) return;
    tries++;
    try {
      const d = await fetch(Config.http + '/api/register/check-bounce?email=' + encodeURIComponent(email)).then(r => r.json());
      if (d.status === 'ok' && d.data && d.data.bounced) {
        $('rErr').textContent = '验证码发送失败：邮箱地址不存在，请检查填写正确';
        toast('验证码发送失败，请检查邮箱填写正确', false);
        codeCooldown = 0;
        const btn = $('rCodeBtn');
        btn.textContent = '获取验证码'; btn.disabled = false;
        return;
      }
    } catch (e) { /* network hiccup: keep polling */ }
    setTimeout(tick, 3500);
  };
  setTimeout(tick, 2000);
}

async function doRegister() {
  const btn = $('rBtn'); btn.disabled = true; $('rErr').textContent = '';
  try {
    const d = await http('/api/register', {
      username: $('rUser').value.trim(),
      password: $('rPass').value,
      password2: $('rPass2').value,
      email: $('rEmail').value.trim(),
      code: $('rCode').value.trim(),
    });
    S.token = d.token; S.me = d.user;
    localStorage.setItem('nova_token', d.token);
    saveAccount(d.user, d.token);
    localStorage.removeItem('kc_acct_add');
    enterApp();
  } catch (e) { $('rErr').textContent = e.message; }
  finally { btn.disabled = false; }
}

function logout() {
  localStorage.removeItem('nova_token');
  if (S.ws) S.ws.close();
  location.reload();
}

// ============================================================ websocket
function connect() {
  S.connAttempted = true;   // banner only meaningful after a real attempt
  // A half-open socket can sit in CONNECTING forever after a network drop
  // (some WebViews never fire onerror/onclose for it) — abandon it so the
  // reconnect loop can actually make progress.
  if (S.ws && S.ws.readyState === WebSocket.CONNECTING &&
      S.wsStartedAt && Date.now() - S.wsStartedAt > 10000) {
    try { S.ws.close(); } catch (_) {}
    S.ws = null;
  }
  if (S.ws && (S.ws.readyState === WebSocket.OPEN || S.ws.readyState === WebSocket.CONNECTING)) return;
  let ws;
  S.wsStartedAt = Date.now();
  try { ws = new WebSocket(Config.ws); }
  catch (e) { setConnected(false); scheduleReconnect(); return; }
  S.ws = ws;

  ws.onopen = () => {
    ws.send(JSON.stringify({ op: 'auth', token: S.token }));
  };
  ws.onmessage = (ev) => {
    let frame;
    try { frame = JSON.parse(ev.data); } catch (e) { return; }
    handleFrame(frame);
  };
  ws.onclose = () => {
    setConnected(false);
    scheduleReconnect();
  };
  ws.onerror = () => {
    // mark disconnected immediately (onclose may be delayed in WebViews)
    setConnected(false);
    try { ws.close(); } catch (e) { }
  };
}
// fixed-interval reconnect (interval comes from the admin panel)
function scheduleReconnect() {
  clearTimeout(S.reconnectTimer);
  S.nextRetryIn = S.reconnectInterval;
  renderServerBanner();
  S.reconnectTimer = setTimeout(() => { connect(); }, S.reconnectInterval * 1000);
}
function manualReconnect() {
  clearTimeout(S.reconnectTimer);
  S.nextRetryIn = 0;
  connect();
}

// ---- global "cannot reach server" banner (visible on every screen) ----
// Swipe up on the banner to dismiss it; it reappears only after a fresh
// connection attempt fails again (dismissed flag resets on reconnect success).
function attachSwipeDismiss(banner, onDismiss) {
  if (banner._swipeBound) return;
  banner._swipeBound = true;
  let startY = null;
  const start = (y) => { startY = y; };
  const move = (y) => {
    if (startY === null) return;
    const dy = startY - y;
    if (dy > 8) banner.style.transform = `translateY(${-dy}px)`;
  };
  const end = (y) => {
    if (startY === null) return;
    const dy = startY - y;
    startY = null;
    banner.style.transform = '';
    if (dy > 40) onDismiss();
  };
  banner.addEventListener('touchstart', (e) => start(e.touches[0].clientY), { passive: true });
  banner.addEventListener('touchmove', (e) => move(e.touches[0].clientY), { passive: true });
  banner.addEventListener('touchend', (e) => end(e.changedTouches[0].clientY));
  // mouse drag for desktop
  banner.addEventListener('mousedown', (e) => start(e.clientY));
  banner.addEventListener('mousemove', (e) => { if (e.buttons === 1) move(e.clientY); });
  banner.addEventListener('mouseup', (e) => end(e.clientY));
  banner.addEventListener('mouseleave', () => { startY = null; banner.style.transform = ''; });
}

function renderServerBanner() {
  let banner = document.getElementById('serverBanner');
  if (!banner) {
    banner = document.createElement('div');
    banner.id = 'serverBanner';
    banner.className = 'server-banner';
    banner.innerHTML = '<span id="serverBannerText"></span>'
      + '<button class="banner-btn" onclick="manualReconnect()">立即重试</button>';
    document.body.appendChild(banner);
    attachSwipeDismiss(banner, () => { S.serverBannerDismissed = true; banner.style.display = 'none'; });
  }
  if (S.connected) { banner.style.display = 'none'; return; }
  // never show the banner before the app actually tried to connect
  // (e.g. on the login screen — that was a misleading false alarm)
  if (!S.connAttempted) { banner.style.display = 'none'; return; }
  if (S.serverBannerDismissed) { banner.style.display = 'none'; return; }
  const txt = document.getElementById('serverBannerText');
  if (txt) {
    txt.textContent = S.nextRetryIn > 0
      ? `连接失败 · ${S.nextRetryIn}s 后自动重试`
      : '连接失败，正在重试…';
  }
  banner.style.display = 'flex';
}
// countdown ticker for the banner
setInterval(() => {
  if (S.connected) return;
  if (S.nextRetryIn > 0) { S.nextRetryIn--; }
  renderServerBanner();
}, 1000);

// ---- theme: light / dark / follow system ----
function applyTheme(mode) {
  if (mode !== 'light' && mode !== 'dark' && mode !== 'auto') mode = 'auto';
  document.documentElement.setAttribute('data-theme', mode);
  try { localStorage.setItem('kc_theme', mode); } catch (e) { }
  updateThemeBtn();
}
function cycleTheme() {
  const order = ['light', 'dark', 'auto'];
  const cur = document.documentElement.getAttribute('data-theme') || 'auto';
  applyTheme(order[(order.indexOf(cur) + 1) % 3]);
}
function updateThemeBtn() {
  const mode = document.documentElement.getAttribute('data-theme') || 'auto';
  const labels = { light: '☀ 浅色', dark: '☾ 深色', auto: '◐ 跟随系统' };
  const icons = { light: '☀', dark: '☾', auto: '◐' };
  // header button is icon-only (text never fits the round button — that
  // overflow is what made it look broken); full label goes to the title
  const b = document.getElementById('themeBtn');
  if (b) { b.textContent = icons[mode]; b.title = '切换主题（当前：' + labels[mode] + '）'; }
  const b2 = document.getElementById('themeBtnAuth');
  if (b2) { b2.textContent = labels[mode]; b2.title = '切换主题'; }
}
function setConnected(on) {
  const wasConnected = S.connected;
  S.connected = on;
  if (on && !wasConnected) {
    clearTimeout(S.reconnectTimer); S.nextRetryIn = 0;
    S.serverBannerDismissed = false;  // fresh connection: allow banner again on future failures
  }
  const dot = $('connDot');
  if (dot) { dot.className = 'conn' + (on ? '' : ' off'); dot.title = on ? '已连接' : '连接断开，重连中…'; }
  renderServerBanner();
}

function request(op, params = {}, timeoutMs = 15000) {
  return new Promise((resolve, reject) => {
    if (!S.ws || S.ws.readyState !== WebSocket.OPEN) { reject(new Error('未连接服务器')); return; }
    const id = ++S.reqId;
    S.pending[id] = { resolve, reject };
    S.ws.send(JSON.stringify({ op, req_id: id, ...params }));
    setTimeout(() => {
      if (S.pending[id]) { delete S.pending[id]; reject(new Error('请求超时')); }
    }, timeoutMs);
  });
}

// ============================================================ frame handling
function handleFrame(frame) {
  if (frame.op === 'auth_ok') {
    setConnected(true);
    S.me = frame.user;
    S.sessions = frame.sessions || [];
    S.friends = frame.friends || [];
    S.requests = frame.requests || [];
    S.bridgeConnected = frame.server ? !!frame.server.bridge_connected : true;
    if (frame.server && frame.server.app_name) Config.appName = frame.server.app_name;
    renderMe(); renderSessions(); renderFriends(); renderBadge(); renderBridgeBanner();
    // open newest session on desktop
    if (!isMobile() && S.sessions.length) openSession(S.sessions[0].id);
    return;
  }
  if (frame.op === 'auth_failed') { toast('登录已过期，请重新登录', false); logout(); return; }
  if (frame.op === 'error') { toast(frame.msg || '服务器错误', false); return; }
  if (frame.op === 'result') {
    const p = S.pending[frame.req_id];
    if (p) {
      delete S.pending[frame.req_id];
      if (frame.status === 'ok') p.resolve(frame.data || {});
      else p.reject(new Error(frame.msg || '操作失败'));
    }
    return;
  }
  // OneBot-style events
  if (frame.post_type === 'message') { onIncomingMessage(frame); return; }
  if (frame.post_type === 'notice') { onNotice(frame); return; }
  if (frame.post_type === 'request') { onRequest(frame); return; }
  if (frame.post_type === 'meta_event') { onMeta(frame); return; }
}

function _consumeLocalEcho(sid, m) {
  // match the optimistic bubble with the server echo so it shows only once
  const list = S.messages[sid] || [];
  for (let i = list.length - 1; i >= 0; i--) {
    const lm = list[i];
    if (lm.__local && lm.raw_message === m.raw_message) {
      lm.__local = false;
      lm.message_id = m.message_id;
      lm.time = m.time;
      return true;
    }
  }
  return false;
}

function onIncomingMessage(m) {
  const sid = m.session_id;
  // own message echoed back from the server: the optimistic bubble is
  // already on screen — confirm it instead of appending a duplicate
  if (m.sender && S.me && m.sender.user_id === S.me.user_id && _consumeLocalEcho(sid, m)) {
    const sess = S.sessions.find(s => s.id === sid);
    if (sess) {
      sess.last_msg_ts = m.time;
      sess.last_msg_preview = m.raw_message || '';
      sortSessions(); renderSessions();
    }
    saveCache();
    return;
  }
  (S.messages[sid] = S.messages[sid] || []).push(m);
  const sess = S.sessions.find(s => s.id === sid);
  if (sess) {
    sess.last_msg_ts = m.time;
    sess.last_msg_preview = m.raw_message || '';
    sortSessions();
  } else {
    // unknown session (e.g. external QQ) — refetch will happen via notice; create stub
    S.sessions.unshift({ id: sid, kind: 'ai', name: '会话', last_msg_ts: m.time, last_msg_preview: m.raw_message || '' });
  }
  renderSessions();
  if (S.activeSession === sid) {
    appendMessage(m);
    if (m.sender.user_id !== S.me.user_id) scrollToBottom();
  }
  if (m.sender.user_id !== S.me.user_id && S.activeSession !== sid) {
    toast(`${nameOf(m.sender.user_id)}: ${(m.raw_message || '').slice(0, 40)}`);
  }
  saveCache();
}

function onNotice(n) {
  switch (n.notice_type) {
    case 'bot_typing':
      S.botTyping[n.session_id] = n.typing;
      if (S.activeSession === n.session_id) renderTyping();
      break;
    case 'typing':
      S.typing[n.session_id] = n.typing;
      if (S.activeSession === n.session_id) renderTyping();
      break;
    case 'session_created':
      if (!S.sessions.find(s => s.id === n.session.id)) S.sessions.unshift(n.session);
      renderSessions();
      if (n.session.peer && !S.friends.find(f => f.user_id === n.session.peer.user_id)) {
        // direct session with peer info
      }
      break;
    case 'session_deleted':
      S.sessions = S.sessions.filter(s => s.id !== n.session_id);
      if (S.activeSession === n.session_id) { S.activeSession = null; showEmpty(); }
      renderSessions();
      break;
    case 'friend_added':
      if (!S.friends.find(f => f.user_id === n.user_id)) {
        S.friends.push({ user_id: n.user_id, nickname: n.nickname, avatar_color: n.avatar_color, avatar: n.avatar || '', online: n.online, signature: '' });
      }
      S.requests = S.requests.filter(r => r.user_id !== n.user_id);
      renderFriends(); renderBadge(); renderSessions();
      toast(`你和 ${n.nickname} 已成为好友`);
      break;
    case 'avatar_changed': {
      // someone changed their avatar — update local copies everywhere
      if (S.me && S.me.user_id === n.user_id) { S.me.avatar = n.avatar; renderMe(); }
      const f = S.friends.find(f => f.user_id === n.user_id);
      if (f) f.avatar = n.avatar;
      S.sessions.forEach(s => { if (s.peer && s.peer.user_id === n.user_id) s.peer.avatar = n.avatar; });
      renderFriends(); renderSessions();
      if (profileData && profileData.user_id === n.user_id) renderAvatar($('pfAv'), profileData);
      break;
    }
    case 'friend_removed': {
      const f = S.friends.find(f => f.user_id === n.user_id);
      S.friends = S.friends.filter(f => f.user_id !== n.user_id);
      renderFriends(); renderSessions();
      if (f) toast(`已删除好友 ${f.nickname}`);
      break;
    }
    case 'friend_rejected':
      toast('好友申请被拒绝', false);
      break;
  }
}

function onRequest(r) {
  if (r.request_type === 'friend') {
    if (!S.requests.find(q => q.user_id === r.user_id)) {
      S.requests.push({ user_id: r.user_id, nickname: '用户' + r.user_id, time: r.time });
    }
    renderFriends(); renderBadge();
    toast('收到新的好友申请');
  }
}

function onMeta(m) {
  if (m.meta_event_type === 'bridge') {
    const nowOn = !!m.connected;
    if (S.bridgeConnected !== nowOn) {
      S.bridgeConnected = nowOn;
      // a state change invalidates a previous dismissal
      S.bridgeBannerDismissed = false;
      renderBridgeBanner();
      if (nowOn) toast('WS 已连接');
      // no error toast on disconnect — the banner (only inside AI chats) covers it
    }
    return;
  }
  if (m.meta_event_type === 'presence') {
    const f = S.friends.find(f => f.user_id === m.user_id);
    if (f) f.online = m.online;
    renderFriends(); renderSessions();
    if (S.activeSession) {
      const sess = S.sessions.find(s => s.id === S.activeSession);
      if (sess && sess.peer && sess.peer.user_id === m.user_id) {
        sess.peer.online = m.online;
        renderHead(sess);
      }
    }
  }
}

// ============================================================ bridge banner
// Shown ONLY while an AI session (needs AstrBot) is open and the bridge is
// down — not globally. Swipe up to dismiss until the bridge state changes.
function renderBridgeBanner() {
  let banner = document.getElementById('bridgeBanner');
  const sess = S.sessions.find(s => s.id === S.activeSession);
  const needBanner = !S.bridgeConnected && sess && sess.kind === 'ai' && !S.bridgeBannerDismissed;
  if (needBanner) {
    if (!banner) {
      banner = document.createElement('div');
      banner.id = 'bridgeBanner';
      banner.className = 'bridge-banner';
      banner.innerHTML = '<span id="bridgeBannerText">连接失败，AI 暂时无法回复</span>'
        + '<button id="bridgeRetryBtn" class="banner-btn">重新连接</button>';
      document.body.appendChild(banner);
      document.getElementById('bridgeRetryBtn').onclick = retryBridgeConnect;
      attachSwipeDismiss(banner, () => { S.bridgeBannerDismissed = true; banner.style.display = 'none'; });
    }
    banner.style.display = 'flex';
  } else if (banner) {
    banner.style.display = 'none';
  }
}

async function retryBridgeConnect() {
  const btn = document.getElementById('bridgeRetryBtn');
  const txt = document.getElementById('bridgeBannerText');
  if (!btn) return;
  btn.disabled = true; btn.textContent = '连接中…';
  try {
  const r = await request('reconnect_bridge', {}, 30000);
  if (S.bridgeConnected) { toast('WS 已连接'); }
  else { toast('连接失败', false); }
  } catch (e) {
    toast('连接失败', false);
  } finally {
    btn.disabled = false; btn.textContent = '重新连接';
  }
}

// ============================================================ rendering
// Render an avatar into `el`. obj = {avatar, avatar_color, nickname}.
// withDot appends an online dot (shown when `online`). `avatar` is a
// server URL like "/avatar/3.jpg?v=..." ("" = colored initial fallback).
function renderAvatar(el, obj, withDot = false, online = false) {
  if (!el) return;
  let html;
  if (obj && obj.avatar) {
    html = `<img class="av-img" src="${Config.http}${obj.avatar}" alt="">`;
    el.style.background = 'transparent';
  } else {
    html = esc(((obj && obj.nickname) || '?')[0]);
    el.style.background = (obj && obj.avatar_color) || '#7C6CF0';
  }
  if (withDot) {
    html += `<span class="on-dot${obj && obj.avatar ? ' on-img' : ''}" style="display:${online ? '' : 'none'}"></span>`;
  }
  el.innerHTML = html;
}

function renderMe() {
  // top-left avatar doubles as the personal-info entry (self = always online)
  renderAvatar($('sbMeAv'), S.me, true, true);
  const t = $('sbTitle'); if (t) t.textContent = Config.appName;
  $('authAppName').textContent = Config.appName;
  document.title = Config.appName;
}

function sortSessions() {
  S.sessions.sort((a, b) => (b.last_msg_ts || 0) - (a.last_msg_ts || 0));
}

function sessionIcon(s) {
  if (s.kind === 'direct') {
    if (s.external_qq) return { txt: 'Q', color: '#4C9BE8', avatar: '' };
    return {
      txt: (s.peer ? s.peer.nickname[0] : '?'),
      color: s.peer ? s.peer.avatar_color : '#8d8aa8',
      avatar: s.peer ? (s.peer.avatar || '') : '',
    };
  }
  return { txt: 'K', color: '#0bb987', img: 'logo.png' };
}

function renderSessions() {
  sortSessions();
  const box = $('sessionList');
  if (!S.sessions.length) {
    box.innerHTML = '<div class="empty-tip">还没有会话<br>点击上方「新建对话」开始</div>';
    return;
  }
  box.innerHTML = S.sessions.map(s => {
    const icon = sessionIcon(s);
    const title = s.kind === 'direct' ? (s.peer ? s.peer.nickname : (s.name || '好友会话')) : s.name;
    const onlineDot = (s.kind === 'direct' && s.peer && s.peer.online) ? '<span class="on-dot"></span>' : '';
    const del = s.kind === 'ai' ? `<button class="del-btn" onclick="event.stopPropagation();deleteSession('${s.id}')" title="删除会话">✕</button>` : '';
    let avInner;
    if (icon.img) avInner = `<img class="av av-img" src="${icon.img}" alt="">${onlineDot}`;
    else if (icon.avatar) avInner = `<div class="av" style="background:transparent">${`<img class="av-img" src="${Config.http}${icon.avatar}" alt="">`}${onlineDot}</div>`;
    else avInner = `<div class="av" style="background:${icon.color}">${esc(icon.txt)}${onlineDot}</div>`;
    return `<div class="sess-item ${S.activeSession === s.id ? 'active' : ''}" onclick="openSession('${s.id}')">
      ${avInner}
      <div class="meta">
        <div class="t1"><span>${esc(title)}</span><time>${s.last_msg_ts ? fmtTime(s.last_msg_ts) : ''}</time></div>
        <div class="t2">${esc(s.last_msg_preview || (s.kind === 'ai' ? 'AI 对话' : '点击开始聊天'))}</div>
      </div>${del}</div>`;
  }).join('');
}

// avatar HTML snippet from {avatar, avatar_color, nickname}
function avSnippet(o) {
  if (o.avatar) {
    return `<img class="av-img" src="${Config.http}${o.avatar}" alt="" style="background:transparent">`;
  }
  return esc(((o.nickname) || '?')[0]);
}

function renderFriends() {
  const box = $('friendList');
  let html = '';
  if (S.requests.length) {
    html += '<div style="font-size:12px;color:var(--muted);padding:6px 8px">好友申请</div>';
    html += S.requests.map(r => `<div class="req-item">
      <div class="av" style="background:${r.avatar ? 'transparent' : (r.avatar_color || '#8d8aa8')}">${avSnippet(r)}</div>
      <div class="meta"><div class="n">${esc(r.nickname)}</div><div class="c">请求添加你为好友</div></div>
      <div><button class="yes" onclick="handleFriend(${r.user_id},true)">同意</button><button class="no" onclick="handleFriend(${r.user_id},false)">拒绝</button></div>
    </div>`).join('');
  }
  if (S.friends.length) {
    html += '<div style="font-size:12px;color:var(--muted);padding:6px 8px">我的好友（' + S.friends.length + '）</div>';
    html += S.friends.map(f => `<div class="friend-item" onclick="openUserProfile(${f.user_id})" oncontextmenu="delFriendMenu(event,${f.user_id})">
      <div class="av" style="background:${f.avatar ? 'transparent' : f.avatar_color}">${avSnippet(f)}${f.online ? '<span class="on-dot"></span>' : ''}</div>
      <div class="meta"><div class="n">${esc(f.nickname)}</div><div class="s">${f.online ? '在线' : '离线'}</div></div>
    </div>`).join('');
  }
  if (!html) html = '<div class="empty-tip">还没有好友<br>在上方输入用户名添加</div>';
  box.innerHTML = html;
}

function renderBadge() {
  const html = S.requests.length ? `<span class="dot-badge">${S.requests.length}</span>` : '';
  $('reqBadge').innerHTML = html;
  const bb = $('bbReqBadge'); if (bb) bb.innerHTML = html;
}

function switchTab(which) {
  $('tabChat').classList.toggle('active', which === 'chat');
  $('tabFriend').classList.toggle('active', which === 'friends');
  $('chatTab').style.display = which === 'chat' ? 'flex' : 'none';
  $('friendTab').style.display = which === 'friends' ? 'flex' : 'none';
  // mobile bottom tab bar stays in sync
  const bbC = $('bbChat'), bbF = $('bbFriend');
  if (bbC) bbC.classList.toggle('active', which === 'chat');
  if (bbF) bbF.classList.toggle('active', which === 'friends');
}

// ============================================================ chat
function showEmpty() {
  $('chatEmpty').style.display = 'flex';
  $('chatBox').style.display = 'none';
  $('appView').classList.remove('in-chat');
  renderBridgeBanner();   // leaving the AI chat hides the bridge banner
}

async function openSession(sid) {
  S.activeSession = sid;
  S.lastDay = '';
  const sess = S.sessions.find(s => s.id === sid);
  $('chatEmpty').style.display = 'none';
  $('chatBox').style.display = 'flex';
  $('appView').classList.add('in-chat');
  $('msgArea').innerHTML = '<div class="empty-tip">加载消息中…</div>';
  renderHead(sess);
  renderSessions();
  renderBridgeBanner();   // AI session + bridge down -> show banner here only
  closeSidebarMobile();
  try {
    const d = await request('history', { session_id: sid, limit: 60 });
    if (S.activeSession !== sid) return;
    S.messages[sid] = d.messages || [];
    renderAllMessages();
    scrollToBottom(true);
  } catch (e) { toast(e.message, false); }
}

function renderHead(sess) {
  if (!sess) return;
  const av = $('headAv');
  if (sess.kind === 'ai') {
    av.textContent = '';
    av.style.background = 'transparent';
    av.innerHTML = '<img src="logo.png" alt="" style="width:100%;height:100%;object-fit:cover;border-radius:inherit">';
    $('headName').textContent = sess.name || '新对话';
    $('headSub').textContent = 'AI 助手';
    $('headRename').style.display = 'flex';
  } else if (sess.peer) {
    renderAvatar(av, sess.peer);
    $('headName').textContent = sess.peer.nickname;
    $('headSub').textContent = sess.peer.online ? '在线' : '离线';
    $('headSub').style.color = sess.peer.online ? 'var(--green)' : '';
    $('headRename').style.display = 'none';
  } else {
    av.textContent = 'Q';
    av.style.background = '#4C9BE8';
    $('headName').textContent = sess.name || '外部会话';
    $('headSub').textContent = 'OneBot 外部用户';
    $('headRename').style.display = 'none';
  }
  renderTyping();
}

function renderTyping() {
  const tip = $('typingTip');
  if (!S.activeSession) { tip.textContent = ''; return; }
  if (S.botTyping[S.activeSession]) tip.textContent = '正在输入…';
  else if (S.typing[S.activeSession]) tip.textContent = '对方正在输入…';
  else tip.textContent = '';
}

function imgSrc(d) {
  const src = d.url || d.file || '';
  // OneBot/AstrBot delivers inline images as base64://<data> — a raw
  // <img src> can't load that, so it fell back to "[图片]". Convert to
  // a data: URI (mime sniffed from the base64 magic prefix).
  if (src.startsWith('base64://')) {
    const b64 = src.slice(9);
    let mime = 'image/png';
    if (b64.startsWith('/9j/')) mime = 'image/jpeg';
    else if (b64.startsWith('R0lG')) mime = 'image/gif';
    else if (b64.startsWith('UklGR')) mime = 'image/webp';
    return `data:${mime};base64,${b64}`;
  }
  return src;
}

function segHtml(seg, me) {
  const d = seg.data || {};
  switch (seg.type) {
    case 'text': return esc(d.text || '');
    case 'image': return `<img src="${esc(imgSrc(d))}" loading="lazy" onerror="this.outerHTML='[图片]'">`;
    case 'face': {
      const id = parseInt(d.id || '0', 10);
      return `<span class="face">${['😀','😄','😂','🤣','😅','😊','😍','😘','😎','🤔','😢','😭','😡','👍','👏','🎉','❤️','💔','🌹','☕'][id % 20]}</span>`;
    }
    case 'record': return '🎤 [语音消息]';
    case 'video': return '🎬 [视频]';
    case 'file': return `📎 [文件] ${esc(d.name || '')}`;
    case 'at': return `<b>@${esc(d.name || d.qq || '')}</b>`;
    case 'reply': return '';
    case 'forward': return ''; // rendered as card
    default: return `[${esc(seg.type)}]`;
  }
}

function forwardCard(seg) {
  const d = seg.data || {};
  const nodes = (d.content || []).slice(0, 4);
  const count = (d.content || []).length;
  const rows = nodes.map(n => {
    const content = Array.isArray(n.content) ? n.content : [];
    const txt = content.map(s => s.type === 'text' ? (s.data.text || '') : '[消息]').join('').replace(/\n/g, ' ');
    return `<div class="fn"><span class="nm">${esc(n.name || '?')}</span><span class="tx">${esc(txt.slice(0, 40))}</span></div>`;
  }).join('');
  const store = encodeURIComponent(JSON.stringify({ title: d.title || '合并转发', nodes: d.content || [] }));
  return `<div class="fwd-card" onclick="showForward(decodeURIComponent('${store.replace(/'/g, '%27')}'))">
    <div class="fh">${esc(d.title || '合并转发')}</div>${rows}
    <div class="ff">${count} 条消息 · 点击查看</div></div>`;
}

function messageHtml(m) {
  const me = S.me && m.sender.user_id === S.me.user_id;
  const segs = Array.isArray(m.message) ? m.message : [];
  const hasForward = segs.some(s => s.type === 'forward');
  const isSystem = m.sender.user_id === 0 && segs.every(s => s.type === 'text') && false;

  const parts = [];
  for (const seg of segs) {
    if (seg.type === 'forward') parts.push(forwardCard(seg));
    else if (seg.type === 'node') { /* nodes outside forward: show inline */
      const txt = (Array.isArray(seg.data?.content) ? seg.data.content : []).map(s => s.type === 'text' ? esc(s.data?.text || '') : '').join('');
      parts.push(`<div class="fn"><span class="nm">${esc(seg.data?.name || '?')}</span><span class="tx">${txt}</span></div>`);
    }
    else parts.push(segHtml(seg, me));
  }
  const body = hasForward ? parts.join('') : parts.join('');
  const day = dayKey(m.time);
  let sep = '';
  if (day !== S.lastDay) {
    S.lastDay = day;
    const d = new Date(m.time * 1000);
    sep = `<div class="day-sep"><span>${d.toDateString() === new Date().toDateString() ? '今天' : d.toLocaleDateString('zh-CN')}</span></div>`;
  }
  const avColor = avatarColorOf(m.sender.user_id);
  const isBot = m.sender.user_id === 0;
  const avHtml = isBot
    ? '<img src="logo.png" alt="" style="width:100%;height:100%;object-fit:cover;border-radius:inherit">'
    : esc((nameOf(m.sender.user_id))[0]);
  const senderLine = (!me && m.sender.user_id !== 0) ? `<div class="msg-sender">${esc(nameOf(m.sender.user_id))}</div>` : '';
  const bubbleCls = hasForward ? 'fwd-wrap' : '';
  return `${sep}<div class="msg-row ${me ? 'me' : ''}">
    <div class="av" style="background:${isBot ? 'transparent' : avColor}">${avHtml}</div>
    <div class="msg-body">${senderLine}
      <div class="bubble ${bubbleCls}">${body}</div>
      <div class="msg-time">${fmtClock(m.time)}</div>
    </div></div>`;
}

function renderAllMessages() {
  S.lastDay = '';
  const msgs = S.messages[S.activeSession] || [];
  $('msgArea').innerHTML = msgs.length ? msgs.map(messageHtml).join('') :
    '<div class="empty-tip">暂无消息，发送第一条消息吧</div>';
  renderTypingBubble();
}
function appendMessage(m) {
  const area = $('msgArea');
  const placeholder = area.querySelector('.empty-tip');
  if (placeholder) placeholder.remove();
  area.insertAdjacentHTML('beforeend', messageHtml(m));
  renderTypingBubble();
}
function renderTypingBubble() {
  const area = $('msgArea');
  area.querySelectorAll('.typing-row').forEach(e => e.remove());
  if (S.activeSession && S.botTyping[S.activeSession]) {
    area.insertAdjacentHTML('beforeend',
      `<div class="msg-row typing-row"><div class="av" style="background:transparent"><img src="logo.png" alt="" style="width:100%;height:100%;object-fit:cover;border-radius:inherit"></div>
       <div class="msg-body"><div class="bubble typing-bubble"><i></i><i></i><i></i></div></div></div>`);
  }
}
function scrollToBottom(force) {
  const area = $('msgArea');
  requestAnimationFrame(() => { area.scrollTop = area.scrollHeight; });
}

function showForward(payloadStr) {
  let data;
  try { data = JSON.parse(payloadStr); } catch (e) { return; }
  $('fwdTitle').textContent = data.title || '合并转发';
  $('fwdBody').innerHTML = (data.nodes || []).map(n => {
    const content = Array.isArray(n.content) ? n.content : [];
    const html = content.map(s => s.type === 'text' ? esc(s.data?.text || '') :
      (s.type === 'image' ? `<img src="${esc(imgSrc(s.data || {}))}" style="max-width:200px;border-radius:8px">` : `[${esc(s.type)}]`)).join('<br>');
    const t = n.time ? new Date(n.time * 1000).toLocaleString('zh-CN') : '';
    return `<div class="fi"><div class="av">${esc((n.name || '?')[0])}</div>
      <div class="c"><div class="n">${esc(n.name || '?')}<time>${t}</time></div><div class="t">${html || '&nbsp;'}</div></div></div>`;
  }).join('') || '<div class="empty-tip">空转发</div>';
  $('fwdModal').style.display = 'flex';
}

// ============================================================ actions
async function sendMessage() {
  const input = $('msgInput');
  const text = input.value.trim();
  if (!text || !S.activeSession) return;
  input.value = '';
  autoGrow();
  // optimistic append — flagged so the server echo is merged, not duplicated
  const m = {
    session_id: S.activeSession, sender: { user_id: S.me.user_id, nickname: S.me.nickname },
    message: [{ type: 'text', data: { text } }], raw_message: text, time: Date.now() / 1000,
    __local: true,
  };
  (S.messages[S.activeSession] = S.messages[S.activeSession] || []).push(m);
  appendMessage(m); scrollToBottom();
  try {
    await request('message', { session_id: S.activeSession, message: text });
  } catch (e) { toast(e.message, false); }
}

async function createSession() {
  try {
    const d = await request('create_session', { kind: 'ai', name: '新对话' });
    S.sessions.unshift(d.session);
    renderSessions();
    openSession(d.session.id);
  } catch (e) { toast(e.message, false); }
}

async function deleteSession(sid) {
  if (!confirm('删除该会话及其所有消息？')) return;
  try {
    await request('delete_session', { session_id: sid });
  } catch (e) { toast(e.message, false); }
}

async function renameSession() {
  const sess = S.sessions.find(s => s.id === S.activeSession);
  if (!sess) return;
  // custom modal — the native prompt() leaks the page URL (server IP)
  // in its title bar on some WebViews
  $('renameInput').value = sess.name || '';
  $('renameModal').style.display = 'flex';
  setTimeout(() => { $('renameInput').focus(); $('renameInput').select(); }, 60);
}

async function saveRenameSession() {
  const sess = S.sessions.find(s => s.id === S.activeSession);
  const name = $('renameInput').value.trim();
  if (!sess || !name) return;
  try {
    await request('rename_session', { session_id: sess.id, name });
    sess.name = name;
    renderSessions(); renderHead(sess);
    $('renameModal').style.display = 'none';
  } catch (e) { toast(e.message, false); }
}

async function addFriend() {
  const name = $('faUser').value.trim();
  if (!name) return;
  try {
    const d = await request('friend_add', { username: name });
    if (d.direct) toast(`你和 ${name} 已是好友，会话已创建`);
    else toast('好友申请已发送');
    $('faUser').value = '';
  } catch (e) { toast(e.message, false); }
}

async function handleFriend(userId, approve) {
  try {
    await request('friend_handle', { user_id: userId, approve });
    S.requests = S.requests.filter(r => r.user_id !== userId);
    renderBadge(); renderFriends();
    toast(approve ? '已添加好友' : '已拒绝');
  } catch (e) { toast(e.message, false); }
}

async function openDirect(userId) {
  // find existing direct session with this peer
  const sess = S.sessions.find(s => s.kind === 'direct' && s.peer && s.peer.user_id === userId);
  if (sess) { openSession(sess.id); return; }
  // ask server by sending a message to create: use friend_add idempotent path
  try {
    const d = await request('friend_add', { username: nameOf(userId) });
    const s2 = S.sessions.find(s => s.kind === 'direct' && s.peer && s.peer.user_id === userId);
    if (s2) openSession(s2.id);
    else toast('会话创建中…');
  } catch (e) { toast(e.message, false); }
}

function delFriendMenu(ev, userId) {
  ev.preventDefault();
  const f = S.friends.find(f => f.user_id === userId);
  if (f && confirm(`删除好友 ${f.nickname}？`)) {
    request('friend_delete', { user_id: userId }).catch(e => toast(e.message, false));
  }
}

// ============================================================ composer UX
function autoGrow() {
  const t = $('msgInput');
  t.style.height = 'auto';
  t.style.height = Math.min(t.scrollHeight, 130) + 'px';
}
$('msgInput').addEventListener('input', autoGrow);
// Enter behavior differs per platform:
//  - desktop: Enter sends, Shift+Enter newline
//  - mobile (touch): Enter = newline (mobile keyboards have their own send,
//    and a phone Enter should never fire the message by accident)
const _isTouch = ('ontouchstart' in window) || (navigator.maxTouchPoints > 0) ||
  (window.matchMedia && window.matchMedia('(pointer: coarse)').matches);
$('msgInput').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') {
    if (_isTouch) { /* mobile: let the newline happen naturally */ return; }
    if (!e.shiftKey) { e.preventDefault(); sendMessage(); }
  }
});

// typing broadcast (throttled)
let lastTypingSent = 0;
$('msgInput').addEventListener('input', () => {
  if (!S.activeSession || Date.now() - lastTypingSent < 3000) return;
  lastTypingSent = Date.now();
  request('typing', { session_id: S.activeSession, typing: true }).catch(() => { });
});

// ============================================================ cache
function saveCache() {
  try {
    if (!S.me) return;
    const slim = {};
    for (const [k, v] of Object.entries(S.messages)) {
      slim[k] = (v || []).slice(-40);
    }
    localStorage.setItem('nova_cache_' + S.me.user_id, JSON.stringify({
      sessions: S.sessions.slice(0, 30), messages: slim, ts: Date.now(),
    }));
  } catch (e) { /* quota */ }
}
function loadCache() {
  try {
    if (!S.me) return false;
    const raw = localStorage.getItem('nova_cache_' + S.me.user_id);
    if (!raw) return false;
    const c = JSON.parse(raw);
    if (Date.now() - c.ts > 7 * 86400 * 1000) return false;
    S.sessions = c.sessions || [];
    S.messages = c.messages || {};
    return true;
  } catch (e) { return false; }
}

// ============================================================ boot
async function enterApp() {
  $('authView').style.display = 'none';
  $('appView').classList.add('show');
  renderMe();
  loadCache();         // instant paint from cache
  renderSessions();
  connect();           // then live sync
  // self-update check (Android APK only, no-op elsewhere)
  try { setTimeout(checkForUpdate, 800); } catch (e) { }
  // restore: user killed the app on the account-switch page → return there
  try {
    if (localStorage.getItem('kc_acct_page') === '1') {
      setTimeout(openAccountPage, 200);
    }
  } catch (e) { }
}

(async function boot() {
  // theme first so the very first paint is correct
  applyTheme(localStorage.getItem('kc_theme') || 'auto');
  // Android native bridge: real status-bar + camera punch-hole height.
  // env(safe-area-inset-top) is 0 in many WebViews, so banners ended up
  // sitting ON the notch. The shell measures window insets and reports them
  // in dp (== CSS px at WebView scale 1). The old physical-px path was
  // inflated on some ROMs (status_bar_height resource includes a phantom
  // notch reservation), which pushed the topbar far below the status bar —
  // measured insets are the ground truth.
  window.__kcApplySafeInsets = function (topDp, bottomDp) {
    try {
      if (topDp >= 0) document.documentElement.style.setProperty('--safe-top', topDp + 'px');
      if (bottomDp > 0) document.documentElement.style.setProperty('--safe-bottom', bottomDp + 'px');
    } catch (e) { }
  };
  try {
    if (window.KCNative && typeof KCNative.topInsetDp === 'function') {
      const top = KCNative.topInsetDp();
      const bottom = (typeof KCNative.bottomInsetDp === 'function') ? KCNative.bottomInsetDp() : -1;
      if (top >= 0) window.__kcApplySafeInsets(top, bottom);
    } else if (window.KCNative && typeof KCNative.topInsetPx === 'function') {
      // legacy shell (< v1.1.1): physical-px API. Less accurate (some ROMs
      // report an inflated status_bar_height), but better than nothing.
      const px = KCNative.topInsetPx();
      if (px > 0) window.__kcApplySafeInsets(Math.round(px / (window.devicePixelRatio || 1)), -1);
    }
  } catch (e) { /* desktop browser / older APK — keep CSS env() fallback */ }
  await loadConfig();
  $('authAppName').textContent = Config.appName;
  // do NOT display the server address in the UI (avoid exposing it)
  $('authServerNote').innerHTML = '';
  document.title = Config.appName;
  // fetch admin-managed client settings (reconnect interval) if reachable
  loadClientSettings();
  // follow OS theme changes while in "auto" mode
  try {
    window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
      if ((document.documentElement.getAttribute('data-theme') || 'auto') === 'auto') applyTheme('auto');
    });
  } catch (e) { }
  if (S.token) {
    // validate token silently by connecting; auth_ok or auth_failed decides
    try {
      const info = await http('/api/session-info');
      if (info.app_name) Config.appName = info.app_name;
    } catch (e) { /* offline: still try cached app */ }
    // try cached session first for instant startup
    try {
      const cached = JSON.parse(localStorage.getItem('nova_me') || 'null');
      if (cached) { S.me = cached; enterApp(); return; }
    } catch (e) { }
  }
  // Reaching here = no valid session to restore: drop the anti-flicker
  // "hide login" class so the login screen actually shows (avoids blank page).
  document.documentElement.classList.remove('kc-booted');
})();

// persist me on auth_ok
const _origHandle = handleFrame;
handleFrame = function (frame) {
  if (frame.op === 'auth_ok' && frame.user) {
    localStorage.setItem('nova_me', JSON.stringify(frame.user));
  }
  return _origHandle(frame);
};

// ============================================================ + menu
function togglePlusMenu(anchor) {
  const m = $('plusMenu');
  if (m.style.display !== 'none') { hidePlusMenu(); return; }
  const r = anchor.getBoundingClientRect();
  m.style.top = Math.min(r.bottom + 6, window.innerHeight - 120) + 'px';
  m.style.left = Math.max(8, Math.min(r.right - 150, window.innerWidth - 160)) + 'px';
  m.style.display = 'block';
  setTimeout(() => document.addEventListener('click', _plusOutside, { once: true }), 0);
}
function _plusOutside(e) {
  const m = $('plusMenu');
  if (m.style.display !== 'none' && !m.contains(e.target)) hidePlusMenu();
  else if (m.style.display !== 'none') setTimeout(() => document.addEventListener('click', _plusOutside, { once: true }), 0);
}
function hidePlusMenu() { $('plusMenu').style.display = 'none'; }
// mobile: tapping the empty chat area opens the drawer (sessions/friends)
function chatEmptyTapped() {
  // mobile home page IS the session list now — nothing to open
}
function openAddFriendEntry() {
  // jump to friends tab (home page on mobile, sidebar tab on desktop)
  switchTab('friends');
  setTimeout(() => { const i = $('faUser'); if (i) i.focus(); }, 250);
}
function backHome() {
  // mobile: leave the conversation overlay and return to the list home page
  hidePlusMenu();
  showEmpty();
}

// mobile hint in the empty-state area (no sidebar visible there)
const _origShowEmpty = showEmpty;
showEmpty = function () {
  _origShowEmpty();
  const hint = $('chatEmptyHint');
  if (hint && window.innerWidth <= 760) {
    hint.innerHTML = '点击右上角 ＋ 添加好友或扫一扫，点击本区域查看会话与好友。<br>所有消息经 OneBot V11 协议实时中转。';
  } else if (hint) {
    hint.innerHTML = '在左侧新建 AI 对话，或从好友列表发起私聊。<br>所有消息经 OneBot V11 协议实时中转。';
  }
};

// ============================================================ overlay pages
function _openOverlay(id) { const p = $(id); p.style.display = 'flex'; return p; }
function _closeOverlay(id) { $(id).style.display = 'none'; }
function _overlayVisible(id) {
  const p = $(id);
  if (!p) return false;
  if (p.style.display !== 'none') return true;   // display-based overlays
  return p.classList.contains('on');              // class-based (myInfoPage)
}

// Android system back button / edge-swipe gesture handler (called from the
// native shell via OnBackPressedCallback). Closes the topmost UI layer and
// returns true when something was closed; returns false when nothing is
// left and the activity should exit.
// Layer order (topmost first):
//   modals/popups -> overlay pages -> open chat -> sidebar drawer ->
//   friends tab (back to chat list)
function __kcHandleBack() {
  try {
    // 1) modals & popups
    const nick = $('nickModal');
    if (nick && nick.style.display !== 'none') { nick.style.display = 'none'; return true; }
    const pm = $('plusMenu');
    if (pm && pm.style.display !== 'none') { hidePlusMenu(); return true; }
    // 2) overlay pages (newest first is fine — only one is open at a time)
    if (_overlayVisible('scanPage')) { closeScanPage(); return true; }
    if (_overlayVisible('myQrPage')) { closeMyQrPage(); return true; }
    if (_overlayVisible('profilePage')) { closeProfilePage(); return true; }
    if (_overlayVisible('accountSwitchPage')) { closeAccountPage(); return true; }
    if (_overlayVisible('myInfoPage')) { closeMyInfoPage(); return true; }
    // 3) inside a conversation -> back to the home list
    if ($('appView') && $('appView').classList.contains('in-chat')) { backHome(); return true; }
    // 4) sidebar drawer open (mobile)
    const sb = $('sidebar');
    if (sb && sb.classList.contains('open')) { closeSidebarMobile(); return true; }
    // 5) on the friends tab -> return to the chat list
    const ft = $('tabFriend');
    if (ft && ft.classList.contains('active')) { switchTab('chat'); return true; }
    return false;   // home list on chat tab -> let the system exit
  } catch (e) {
    return false;
  }
}

// ============================================================ scan page
let scanStream = null, scanTimer = null, scanActive = false;

async function openScanPage() {
  _openOverlay('scanPage');
  $('scanTip').textContent = '正在请求相机权限…';
  try {
    if (!window.isSecureContext) {
      $('scanTip').textContent = '扫码需要 HTTPS 安全环境，请通过 https 访问';
      return;
    }
    scanStream = await navigator.mediaDevices.getUserMedia({
      video: { facingMode: 'environment', width: { ideal: 1280 }, height: { ideal: 720 } },
      audio: false,
    });
    const v = $('scanVideo');
    v.srcObject = scanStream;
    await v.play();
    $('scanTip').textContent = '将对方的名片二维码对准取景框';
    scanActive = true;
    _scanLoop();
  } catch (e) {
    const name = e && e.name;
    if (name === 'NotAllowedError' || name === 'PermissionDeniedError') {
      $('scanTip').textContent = '相机权限被拒绝，请在系统设置中允许 KiteChat 使用相机';
    } else if (name === 'NotFoundError' || name === 'DevicesNotFoundError') {
      $('scanTip').textContent = '未检测到相机设备';
    } else {
      $('scanTip').textContent = '无法打开相机：' + (e && e.message ? e.message : name || '未知错误');
    }
  }
}
function _scanLoop() {
  if (!scanActive) return;
  const v = $('scanVideo');
  if (v.readyState >= 2 && v.videoWidth) {
    const c = $('scanCanvas');
    c.width = v.videoWidth; c.height = v.videoHeight;
    const ctx = c.getContext('2d');
    ctx.drawImage(v, 0, 0);
    let code = null;
    try { code = jsQR(ctx.getImageData(0, 0, c.width, c.height).data, c.width, c.height, { inversionAttempts: 'dontInvert' }); } catch (e) { }
    if (code && code.data) { _onScanResult(code.data); return; }
  }
  scanTimer = setTimeout(_scanLoop, 180);
}
function _onScanResult(text) {
  scanActive = false;
  stopScanStream();
  closeScanPage();
  const m = text.match(/^kitechat:user:(\d+)/);
  if (m) { openUserProfile(parseInt(m[1], 10)); }
  else { toast('不是有效的 KiteChat 名片二维码', false); }
}
function stopScanStream() {
  scanActive = false;
  if (scanTimer) { clearTimeout(scanTimer); scanTimer = null; }
  if (scanStream) { scanStream.getTracks().forEach(t => t.stop()); scanStream = null; }
  const v = $('scanVideo'); if (v) { v.srcObject = null; }
}
function closeScanPage() { stopScanStream(); _closeOverlay('scanPage'); }

// ============================================================ my QR page
function myQrPayload() { return 'kitechat:user:' + S.me.user_id; }
function openMyQrPage() {
  _openOverlay('myQrPage');
  $('qrAv').textContent = (S.me.nickname || '?')[0];
  $('qrAv').style.background = S.me.avatar_color;
  $('qrName').textContent = S.me.nickname;
  const box = $('myQrBox');
  box.innerHTML = '';
  try {
    const qr = qrcode(0, 'M');
    qr.addData(myQrPayload());
    qr.make();
    if (qr.createImgTag) box.innerHTML = qr.createImgTag(6, 12);
    else {
      const n = qr.getModuleCount();
      const cvs = document.createElement('canvas');
      const size = n * 6; cvs.width = size; cvs.height = size;
      const ctx = cvs.getContext('2d');
      ctx.fillStyle = '#fff'; ctx.fillRect(0, 0, size, size);
      ctx.fillStyle = '#000';
      for (let r = 0; r < n; r++) for (let col = 0; col < n; col++) {
        if (qr.isDark(r, col)) ctx.fillRect(col * 6, r * 6, 6, 6);
      }
      box.appendChild(cvs);
    }
  } catch (e) { box.textContent = '二维码生成失败'; }
}
function closeMyQrPage() { _closeOverlay('myQrPage'); }

// ============================================================ user profile page
let profileData = null;
async function openUserProfile(userId) {
  _openOverlay('profilePage');
  $('pfNick').textContent = '加载中…';
  $('pfSub').textContent = ''; $('pfStatus').textContent = '';
  $('pfAddBtn').style.display = 'none';
  $('pfChatBtn').style.display = 'none';
  try {
    const d = await request('user_profile', { user_id: userId });
    profileData = d;
    renderAvatar($('pfAv'), d);
    $('pfNick').textContent = d.nickname;
    $('pfSub').textContent = '@' + d.username + (d.online ? ' · 在线' : '');
    const st = d.friend_status;
    if (d.is_me) {
      $('pfStatus').textContent = '这是你自己';
      $('pfStatus').style.color = 'var(--muted)';
    } else if (st === 'accepted') {
      $('pfStatus').textContent = '已是好友';
      $('pfStatus').style.color = 'var(--green)';
      $('pfChatBtn').style.display = 'block';
    } else if (st === 'pending') {
      $('pfStatus').textContent = '已发送好友请求，等待对方同意';
      $('pfStatus').style.color = 'var(--yellow)';
    } else if (st === 'incoming_pending') {
      $('pfStatus').textContent = '对方向你发起了好友请求，请到好友列表处理';
      $('pfStatus').style.color = 'var(--yellow)';
    } else {
      $('pfStatus').textContent = '添加好友需要对方同意';
      $('pfStatus').style.color = 'var(--muted)';
      $('pfAddBtn').style.display = 'block';
    }
  } catch (e) {
    $('pfNick').textContent = '加载失败';
    toast(e.message, false);
  }
}
async function addFriendFromProfile() {
  if (!profileData) return;
  const btn = $('pfAddBtn');
  btn.disabled = true; btn.textContent = '发送中…';
  try {
    const d = await request('friend_add_id', { user_id: profileData.user_id });
    if (d.direct) { toast('你们已是好友'); }
    else {
      toast('好友请求已发送，等待对方同意');
      $('pfAddBtn').style.display = 'none';
      $('pfStatus').textContent = '已发送好友请求，等待对方同意';
      $('pfStatus').style.color = 'var(--yellow)';
    }
    refreshRequests();
  } catch (e) { toast(e.message, false); }
  finally { btn.disabled = false; btn.textContent = '加为好友'; }
}
async function chatFromProfile() {
  if (!profileData) return;
  closeProfilePage();
  try { await openDirect(profileData.user_id); } catch (e) { toast(e.message, false); }
}
async function refreshRequests() {
  try {
    const d = await request('friend_requests', {});
    S.requests = d.requests || [];
    renderBadge(); renderFriends();
  } catch (e) { }
}
function closeProfilePage() { _closeOverlay('profilePage'); }

// ============================================================ my info page
function openMyInfo() {
  renderAvatar($('miAv'), S.me);
  $('miNick').textContent = S.me.nickname;
  $('miSub').textContent = S.me.online ? '在线' : '';
  $('miUser').textContent = S.me.username || '-';
  $('miEmail').textContent = S.me.email || '-';
  // slide-in-from-left animation (class-driven, see style.css)
  const p = $('myInfoPage');
  p.classList.add('on');
  requestAnimationFrame(() => requestAnimationFrame(() => p.classList.add('slide-in')));
}
function closeMyInfoPage() {
  const p = $('myInfoPage');
  p.classList.remove('slide-in');
  setTimeout(() => p.classList.remove('on'), 240);
}

// ============================================================ avatar change
// Lazy permission model: the <input type="file"> click is what triggers the
// OS picker — camera/gallery permissions are requested by the OS AT THAT
// MOMENT (Android WebChromeClient.onShowFileChooser bridges it), never at
// startup.
function showAvatarMenu() {
  // on Android the accept="image/*" picker offers both camera and gallery
  // in the system chooser; on desktop it opens the file dialog.
  const f = $('avatarFile');
  f.value = '';
  f.click();
}
function onAvatarPicked(input) {
  const file = input.files && input.files[0];
  if (!file) return;
  if (file.size > 8 * 1024 * 1024) { toast('图片太大（限 8MB）', false); return; }
  // downscale client-side to keep the upload small (server crops to 256px)
  const img = new Image();
  const url = URL.createObjectURL(file);
  img.onload = () => {
    const max = 800;
    let w = img.width, h = img.height;
    const sc = Math.min(1, max / Math.max(w, h));
    w = Math.round(w * sc); h = Math.round(h * sc);
    const cvs = document.createElement('canvas');
    cvs.width = w; cvs.height = h;
    cvs.getContext('2d').drawImage(img, 0, 0, w, h);
    URL.revokeObjectURL(url);
    doAvatarUpload(cvs.toDataURL('image/jpeg', 0.85));
  };
  img.onerror = () => { URL.revokeObjectURL(url); toast('无法读取图片', false); };
  img.src = url;
}
async function doAvatarUpload(dataUrl) {
  try {
    toast('正在上传头像…');
    const d = await fetch(Config.http + '/api/avatar/upload', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Auth-Token': S.token },
      body: JSON.stringify({ image_base64: dataUrl }),
    }).then(r => r.json());
    if (d.status !== 'ok') throw new Error(d.msg || '上传失败');
    S.me = d.data.user;
    renderMe();
    renderAvatar($('miAv'), S.me);
    saveCache();
    toast('头像已更新');
  } catch (e) {
    toast(e.message, false);
  }
}
// swipe from the left screen edge → personal info (same as tapping the avatar)
(function attachEdgeSwipe() {
  let sx = null, sy = null;
  const appEl = $('appView');
  appEl.addEventListener('touchstart', (e) => {
    const t = e.touches[0];
    if (t.clientX <= 36) { sx = t.clientX; sy = t.clientY; } else { sx = null; }
  }, { passive: true });
  appEl.addEventListener('touchend', (e) => {
    if (sx === null) return;
    const t = e.changedTouches[0];
    const dx = t.clientX - sx, dy = Math.abs(t.clientY - sy);
    sx = null;
    if (dx <= 70 || dy >= 60) return;
    if ($('appView').classList.contains('in-chat')) return;
    const anyOpen = ['scanPage', 'myQrPage', 'profilePage', 'myInfoPage'].some(
      id => $(id).classList.contains('on') || $(id).style.display === 'flex');
    if (!anyOpen) openMyInfo();
  }, { passive: true });
})();
function openNickEdit() {
  $('nickInput').value = S.me.nickname;
  $('nickModal').style.display = 'flex';
  setTimeout(() => $('nickInput').focus(), 100);
}
async function saveNickname() {
  const nick = $('nickInput').value.trim();
  if (!nick) return;
  try {
    const d = await request('update_profile', { nickname: nick });
    S.me = d.user;
    localStorage.setItem('nova_me', JSON.stringify(S.me));
    $('nickModal').style.display = 'none';
    renderMe(); openMyInfo();
    toast('昵称已更新');
  } catch (e) { toast(e.message, false); }
}
function switchAccount() {
  // now opens the account switch page (list of logged-in + saved accounts)
  _closeOverlay('myInfoPage');
  openAccountPage();
}

// ============================================================ account switch page
// Device-local account store: every account successfully authenticated on
// this device is remembered (token included) so it can be switched to
// quickly. Stored in localStorage, max 10, newest first.
function accountStore() {
  try { return JSON.parse(localStorage.getItem('kc_accounts') || '[]'); } catch (e) { return []; }
}
function saveAccount(user, token) {
  if (!user || !user.username) return;
  const list = accountStore().filter(a => a.username !== user.username);
  list.unshift({
    username: user.username,
    nickname: user.nickname || user.username,
    avatar: user.avatar || '',
    avatar_color: user.avatar_color || '#7C6CF0',
    token: token || S.token || '',
    ts: Date.now(),
  });
  try { localStorage.setItem('kc_accounts', JSON.stringify(list.slice(0, 10))); } catch (e) { }
}
function removeAccount(username) {
  try {
    localStorage.setItem('kc_accounts',
      JSON.stringify(accountStore().filter(a => a.username !== username)));
  } catch (e) { }
  renderAccounts();
}

let acctRenderList = [];   // rows currently shown (index = tap target)
let acctDelTarget = -1;

function openAccountPage() {
  S.acctManage = false;
  const mb = $('acctManageBtn'); if (mb) mb.textContent = '管理';
  // persist: if the app is killed on this page, next launch returns here
  localStorage.setItem('kc_acct_page', '1');
  renderAccounts();
  _openOverlay('accountSwitchPage');
}
function closeAccountPage() {
  localStorage.removeItem('kc_acct_page');
  S.acctManage = false;
  _closeOverlay('accountSwitchPage');
}
function toggleAccountManage() {
  S.acctManage = !S.acctManage;
  $('acctManageBtn').textContent = S.acctManage ? '完成' : '管理';
  renderAccounts();
}
function renderAccounts() {
  acctRenderList = [];
  const cur = S.me;
  if (cur && cur.username) {
    acctRenderList.push({
      current: true, username: cur.username, nickname: cur.nickname || cur.username,
      avatar: cur.avatar || '', avatar_color: cur.avatar_color || '#7C6CF0',
    });
  }
  for (const a of accountStore()) {
    if (cur && a.username === cur.username) continue;   // already shown as current
    acctRenderList.push(a);
  }
  const box = $('acctList');
  if (!acctRenderList.length) { box.innerHTML = '<div class="empty-tip">暂无账号</div>'; return; }
  box.innerHTML = acctRenderList.map((a, i) => acctRowHtml(a, i)).join('');
}
function acctRowHtml(a, i) {
  const avInner = a.avatar
    ? `<img class="av-img" src="${Config.http}${a.avatar}" alt="">`
    : esc(((a.nickname || '?')[0]));
  let right = '';
  if (a.current) right = '<span class="acct-cur">当前</span>';
  else if (S.acctManage) right = '<button class="acct-del" onclick="event.stopPropagation();askDelAccount(' + i + ')">✕</button>';
  return `<div class="acct-item${a.current ? ' current' : ''}" onclick="tapAccount(${i})">
    <div class="av" style="background:${a.avatar ? 'transparent' : (a.avatar_color || '#7C6CF0')}">${avInner}</div>
    <div class="acct-meta"><div class="n">${esc(a.nickname)}</div><div class="u">@${esc(a.username)}</div></div>
    ${right}</div>`;
}
function tapAccount(i) {
  const a = acctRenderList[i];
  if (!a || a.current) return;               // current account: not switchable/deletable
  if (S.acctManage) { askDelAccount(i); return; }
  switchToAccount(i);
}
async function switchToAccount(i) {
  const a = acctRenderList[i];
  if (!a || a.current) return;
  if (!a.token) { toast('该账号没有本机登录凭据，请重新登录', false); return; }
  closeAccountPage();
  if (S.ws) { try { S.ws.close(); } catch (e) { } }
  S.ws = null; S.connected = false; S.connAttempted = false;
  // wipe the previous account's live state before re-auth
  S.sessions = []; S.friends = []; S.requests = [];
  S.messages = {}; S.activeSession = null;
  S.token = a.token;
  localStorage.setItem('nova_token', a.token);
  S.me = { nickname: a.nickname, username: a.username, avatar: a.avatar, avatar_color: a.avatar_color };
  localStorage.setItem('nova_me', JSON.stringify(S.me));
  $('appView').classList.add('show');
  enterApp();
}
function askDelAccount(i) {
  const a = acctRenderList[i];
  if (!a || a.current) return;               // the logged-in account can never be deleted
  acctDelTarget = i;
  $('delAcctName').textContent = a.nickname + '（@' + a.username + '）';
  $('delAcctModal').style.display = 'flex';
}
function confirmDelAccount() {
  const a = acctRenderList[acctDelTarget];
  if (a && !a.current) removeAccount(a.username);
  $('delAcctModal').style.display = 'none';
}
function addAccountFlow() {
  // leave the account page, log out to the login screen; the freshly
  // logged-in account is saved by the auth_ok hook for quick switching.
  localStorage.removeItem('kc_acct_page');
  localStorage.setItem('kc_acct_add', '1');
  _closeOverlay('accountSwitchPage');
  if (S.ws) { try { S.ws.close(); } catch (e) { } }
  S.ws = null; S.connected = false; S.connAttempted = false;
  S.token = ''; S.me = null;
  localStorage.removeItem('nova_token');
  localStorage.removeItem('nova_me');
  document.documentElement.classList.remove('kc-booted');
  $('appView').classList.remove('show');
  $('authView').style.display = 'flex';
  $('lUser').value = ''; $('lPass').value = '';
  showLogin();
}

// ============================================================ mobile topbar sync
const _origRenderMe = renderMe;
renderMe = function () {
  _origRenderMe();
  const mt = $('mtTitle'); if (mt) mt.textContent = Config.appName;
};
const _origLoadClientSettings = loadClientSettings;
loadClientSettings = async function () {
  await _origLoadClientSettings();
  const mt = $('mtTitle'); if (mt) mt.textContent = Config.appName;
};

// ============================================================ in-app self-update (Android APK)
function compareVersions(a, b) {
  // "1.10.0" > "1.9.9"; returns 1 / -1 / 0
  const pa = String(a || '').split('.').map(n => parseInt(n, 10) || 0);
  const pb = String(b || '').split('.').map(n => parseInt(n, 10) || 0);
  for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
    const x = pa[i] || 0, y = pb[i] || 0;
    if (x !== y) return x > y ? 1 : -1;
  }
  return 0;
}

let _updateState = null;   // {mode:'download'|'install_local', manifest|localApk}

async function checkForUpdate() {
  // Android-only (KCNative bridge must exist)
  if (!window.KCNative || typeof KCNative.installedVersion !== 'function') return;
  let installed = '';
  try { installed = KCNative.installedVersion(); } catch (e) { }
  if (!installed) return;

  // 1) scan leftover downloaded APKs.
  //    same version as installed -> deleted natively (returns nothing)
  //    higher version            -> offer "install now"
  let locals = [];
  try { locals = JSON.parse(KCNative.scanUpdates() || '[]'); } catch (e) { }
  if (Array.isArray(locals)) {
    const better = locals.filter(l => l.version && compareVersions(l.version, installed) > 0);
    if (better.length > 0) {
      better.sort((x, y) => compareVersions(x.version, y.version));
      const apk = better[better.length - 1];
      _updateState = { mode: 'install_local', apk };
      $('updateTitle').textContent = '发现新版本';
      $('updateInfo').innerHTML =
        '检测到已下载的新版本安装包 <b>v' + esc(apk.version) + '</b>（当前 v' + esc(installed) + '）<br>是否立即安装？';
      $('updateBtn').textContent = '立即安装';
      $('updateProg').style.display = 'none';
      $('updatePct').style.display = 'none';
      $('updateModal').style.display = 'flex';
      return;
    }
  }

  // 2) ask the server for the newest build
  try {
    const m = await http('/api/apk/latest');
    if (!m || !m.version || !m.filename) return;
    if (compareVersions(m.version, installed) > 0) {
      _updateState = { mode: 'download', manifest: m };
      $('updateTitle').textContent = '发现新版本';
      $('updateInfo').innerHTML =
        '服务器有新版本 <b>v' + esc(m.version) + '</b>（当前 v' + esc(installed) + '）<br>' +
        '大小 ' + (m.size ? (m.size / 1048576).toFixed(1) + ' MB' : '未知') + '，下载后自动安装。';
      $('updateBtn').textContent = '下载更新';
      $('updateProg').style.display = 'none';
      $('updatePct').style.display = 'none';
      $('updateModal').style.display = 'flex';
    }
  } catch (e) { /* server unreachable — skip silently */ }
}

function doUpdate() {
  if (!_updateState) return;
  if (_updateState.mode === 'install_local') {
    try { KCNative.installLocal(_updateState.apk.path); } catch (e) { }
    $('updateModal').style.display = 'none';
    return;
  }
  // download mode
  const m = _updateState.manifest;
  const url = Config.http + '/api/apk/download';
  $('updateBtn').disabled = true;
  $('updateBtn').textContent = '下载中…';
  $('updateProg').style.display = 'block';
  $('updatePct').style.display = 'block';
  $('updateBar').style.width = '0%';
  try {
    KCNative.downloadAndInstall(url, m.filename);
  } catch (e) {
    $('updateInfo').textContent = '启动下载失败：' + e;
    $('updateBtn').disabled = false;
    $('updateBtn').textContent = '重试';
  }
}

function cancelUpdate() {
  $('updateModal').style.display = 'none';
}

// progress callback invoked by the native downloader
function KCUpdateProgress(pct, done, total) {
  if (pct < 0) {
    $('updatePct').textContent = '已下载 ' + (done / 1048576).toFixed(1) + ' MB';
  } else {
    $('updateBar').style.width = pct + '%';
    $('updatePct').textContent = pct + '%' +
      (total > 0 ? '（' + (done / 1048576).toFixed(1) + ' / ' + (total / 1048576).toFixed(1) + ' MB）' : '');
  }
  if (pct >= 100) {
    $('updateBtn').textContent = '准备安装…';
  }
}

function KCUpdateError(msg) {
  $('updateBtn').disabled = false;
  $('updateBtn').textContent = '重试';
  $('updatePct').style.color = 'var(--red)';
  $('updatePct').textContent = '下载失败：' + (msg || '网络错误');
}
