/* Shared by the app pages: language, theme, notices, confirm dialog, sign-out.
   A page adds its own texts with addT({en: {...}, id: {...}}), may define onLang(), then calls boot(). */
var LANG = 'en';
var I18N = {
  en: { app_title: 'Data Check', nav_analyze: 'Analyze', nav_files: 'Files', nav_memory: 'Memory', nav_settings: 'Settings',
    nav_users: 'Users', account: 'Account & password', logout: 'Sign out', theme: 'Light / dark', cancel: 'Cancel', ok: 'OK',
    role_admin: 'administrator', role_user: 'user', err_action: 'That did not work. Please try again.',
    admin_only: 'Only an administrator can change this.' },
  id: { app_title: 'Cek Data', nav_analyze: 'Analisis', nav_files: 'File', nav_memory: 'Memori', nav_settings: 'Pengaturan',
    nav_users: 'Pengguna', account: 'Akun & kata sandi', logout: 'Keluar', theme: 'Terang / gelap', cancel: 'Batal', ok: 'OK',
    role_admin: 'administrator', role_user: 'pengguna', err_action: 'Gagal. Silakan coba lagi.',
    admin_only: 'Hanya administrator yang dapat mengubah ini.' }
};
var IS_ADMIN = !!(window.ME && ME.role === 'admin');
function addT(t) { ['en', 'id'].forEach(function (l) { for (var k in t[l]) I18N[l][k] = t[l][k]; }); }
function tr(k) { return I18N[LANG][k] !== undefined ? I18N[LANG][k] : I18N.en[k]; }
function t(k, p) { var s = tr(k); if (s === undefined) s = k; if (p) for (var x in p) s = s.replace('{' + x + '}', p[x]); return s; }
var $ = function (id) { return document.getElementById(id); };
function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]; }); }
function loc() { return LANG === 'id' ? 'id-ID' : 'en-US'; }
function num(n) { return Number(n).toLocaleString(loc()); }
function bytes(n) { if (n == null) return ''; var u = ['B','KB','MB','GB','TB'], i = 0; while (n >= 1024 && i < 4) { n /= 1024; i++; } return n.toLocaleString(loc(), {maximumFractionDigits: i ? 1 : 0}) + ' ' + u[i]; }
function when(ts) { return ts ? new Date(ts * 1000).toLocaleString(loc(), { dateStyle: 'medium', timeStyle: 'short' }) : ''; }
function store(k, v) { try { if (v === undefined) return localStorage.getItem(k); localStorage.setItem(k, v); } catch (e) { return null; } }
function el(tag, attrs, text) { var e = document.createElement(tag); for (var a in attrs || {}) e.setAttribute(a, attrs[a]); if (text != null) e.textContent = text; return e; }

function setLang(l) {
  LANG = l; document.documentElement.lang = l; store('autoeda-lang', l);
  document.querySelectorAll('[data-t]').forEach(function (e) { e.textContent = t(e.dataset.t); });
  document.querySelectorAll('[data-tp]').forEach(function (e) { e.placeholder = t(e.dataset.tp); });
  document.querySelectorAll('[data-tt]').forEach(function (e) { e.title = t(e.dataset.tt); });
  document.querySelectorAll('[data-setlang]').forEach(function (b) { b.classList.toggle('is-link', b.dataset.setlang === l); });
  if (typeof onLang === 'function') onLang();
}
function toggleTheme() {
  var th = document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
  document.documentElement.setAttribute('data-theme', th); store('autoeda-theme', th);
}
function burger(b) { b.classList.toggle('is-active'); $('nav').classList.toggle('is-active'); }

// a session that ran out sends every call back to the sign-in page
(function () {
  var f = window.fetch;
  window.fetch = function () {
    return f.apply(this, arguments).then(function (r) {
      if (r.status === 401 && location.pathname !== '/login') location.href = '/login?next=' + encodeURIComponent(location.pathname + location.search);
      return r;
    });
  };
})();
function logout() { fetch('/logout', { method: 'POST' }).then(function () { location.href = '/login'; }); }

function toast(msg, kind) {
  var n = el('div', { 'class': 'notification is-' + (kind === 'bad' ? 'danger' : kind === 'good' ? 'success' : 'link') + ' is-light', role: kind === 'bad' ? 'alert' : 'status' });
  var x = el('button', { 'class': 'delete', 'aria-label': t('ok') }); n.appendChild(x); n.appendChild(document.createTextNode(msg));
  var close = function () { if (n.parentNode) n.parentNode.removeChild(n); };
  x.onclick = close; $('toasts').appendChild(n);
  setTimeout(close, kind === 'bad' ? 9000 : 4500);
}
// in-page confirm dialog (instead of the browser's confirm)
var dlgDone = null, dlgFocus = null;
function ask(title, text, yes, danger) {
  return new Promise(function (resolve) {
    dlgFocus = document.activeElement;
    $('dlg-title').textContent = title; $('dlg-text').textContent = text;
    $('dlg-yes').textContent = yes; $('dlg-no').textContent = t('cancel');
    $('dlg-yes').className = 'button ' + (danger ? 'is-danger' : 'is-link');
    $('dlg').classList.add('is-active');
    dlgDone = function (v) { $('dlg').classList.remove('is-active'); dlgDone = null; if (dlgFocus) dlgFocus.focus(); resolve(v); };
    $('dlg-no').focus();
  });
}
document.addEventListener('keydown', function (e) { if (dlgDone && e.key === 'Escape') dlgDone(false); });
function boot() {
  $('dlg-yes').onclick = function () { if (dlgDone) dlgDone(true); };
  $('dlg-no').onclick = function () { if (dlgDone) dlgDone(false); };
  document.querySelectorAll('#dlg .modal-background').forEach(function (b) { b.onclick = function () { if (dlgDone) dlgDone(false); }; });
  document.querySelectorAll('[data-setlang]').forEach(function (b) { b.onclick = function () { setLang(b.dataset.setlang); }; });
  setLang(store('autoeda-lang') || ((navigator.language || '').toLowerCase().indexOf('id') === 0 ? 'id' : 'en'));
}
