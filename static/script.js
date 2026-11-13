const $ = (s, ctx = document) => ctx.querySelector(s);
const $$ = (s, ctx = document) => Array.from(ctx.querySelectorAll(s));

const LABELS = {
  watch: "🍿 Sehenswert",
  pts30: "30+ Pts",
  pts40: "40+ Pts",
  pts50: "50+ Pts",
  tripleDouble: "Triple-Double",
  close4: "Knapp vor Q4",
  closeGame: "Knapp",
  overtime: "Verlängerung",
};

// Browser-Einstellungen pro Benutzer getrennt speichern
const USER = document.body.dataset.user || "";
const store = {
  get(k, d) { try { const v = localStorage.getItem(`${USER}:${k}`); return v === null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem(`${USER}:${k}`, JSON.stringify(v)); } catch {} },
};

// Session abgelaufen -> zurück zur Anmeldung
async function apiFetch(url, opts) {
  const res = await fetch(url, opts);
  if (res.status === 401) { location.replace("/login"); throw new Error("Nicht angemeldet"); }
  return res;
}

const state = {
  games: [],
  filters: new Set(store.get("hc.filters", [])),
  showBadges: store.get("hc.showBadges", false),
  newTab: store.get("hc.newTab", true),
  // "web" = nba.com-Player (Computer, mit Browser-Skript), "app" = NBA-App.
  // Auf Handys sperrt nba.com League-Pass-Videos im Browser ("Open in App") – dort also App.
  playTarget: store.get("hc.playTarget", /iPhone|iPod|Android.+Mobile/i.test(navigator.userAgent) ? "app" : "web"),
  reqId: 0,
};

// ── Abspielen: Browser oder NBA-App ────────────────────────
const PLAY_HINTS = {
  web: "Öffnet den nba.com-Player direkt im „All Possessions“-Stream. Mit dem Browser-Skript (unten) meldet sich dein Browser dort automatisch an, und das Video läuft bildschirmfüllend ohne Zeitleiste.",
  app: "Öffnet das Spiel in der NBA-App (dort bist du meist schon angemeldet). In der App dann „All Possessions“ wählen – ohne App landest du im App Store. Tipp: in der NBA-App „Hide Scores“ einschalten. Auf Handys ist das der einzige Weg – nba.com spielt League Pass dort im Browser nicht ab.",
};
function setPlayTarget(v, rerender = true) {
  state.playTarget = v;
  store.set("hc.playTarget", v);
  $$('input[name="play-target"]').forEach(r => (r.checked = r.value === v));
  $("#play-target-hint").textContent = PLAY_HINTS[v];
  syncBanner();
  if (rerender) render();
}
function syncBanner() {
  $("#nba-login-banner").hidden = state.playTarget !== "web" || store.get("hc.nbaLoginDone", false);
}
$$('input[name="play-target"]').forEach(r => r.addEventListener("change", () => setPlayTarget(r.value)));
$("#banner-done").addEventListener("click", () => { store.set("hc.nbaLoginDone", true); syncBanner(); });
$("#banner-setup").addEventListener("click", () => {
  $("#open-settings").click();
  setTimeout(() => $("#script-section").scrollIntoView({ behavior: "smooth", block: "start" }), 250);
});
$("#install-script").addEventListener("click", async e => {
  const btn = e.currentTarget;
  btn.disabled = true;
  try {
    const { url } = await api("POST", "/api/script/install-link", {});
    location.href = url; // Tampermonkey bzw. Userscripts (iOS) erkennen die .user.js-Seite
  } catch (err) { toast(`Fehler: ${err.message}`); }
  finally { btn.disabled = false; }
});
$("#regen-token").addEventListener("click", async () => {
  if (!confirm("Alle installierten Browser-Skripte sperren? Sie müssen danach auf deinen Geräten neu installiert werden.")) return;
  try { await api("POST", "/api/script/token", {}); toast("Alle Skripte gesperrt – bitte auf deinen Geräten neu installieren"); }
  catch (err) { toast(err.message); }
});
$("#banner-app").addEventListener("click", () => { setPlayTarget("app"); toast("Links öffnen jetzt die NBA-App – änderbar in den Einstellungen"); });

// ── Datum ──────────────────────────────────────────────────
const dateInput = $("#date");

const iso = d => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
const parseIso = s => { const [y, m, d] = s.split("-").map(Number); return new Date(y, m - 1, d); };

function shiftDate(days) {
  const d = parseIso(dateInput.value);
  d.setDate(d.getDate() + days);
  setDate(iso(d));
}

function setDate(value, push = true) {
  dateInput.value = value;
  const d = parseIso(value);
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const diff = Math.round((d - today) / 86400000);
  $("#date-label").textContent = d.toLocaleDateString("de-AT", { weekday: "short", day: "numeric", month: "long", year: "numeric" });
  $("#date-sub").textContent = { 0: "Heute (US-Spieltag)", [-1]: "Letzte Nacht", 1: "Morgen" }[diff] || "US-Spieltag";
  if (push) history.replaceState(null, "", `?date=${value}`);
  loadGames();
}

$("#prev").addEventListener("click", () => shiftDate(-1));
$("#next").addEventListener("click", () => shiftDate(1));
dateInput.addEventListener("change", () => dateInput.value && setDate(dateInput.value));
document.addEventListener("keydown", e => {
  if (e.target.closest("input, dialog")) return;
  if (e.key === "ArrowLeft") shiftDate(-1);
  if (e.key === "ArrowRight") shiftDate(1);
});

// ── Filter ─────────────────────────────────────────────────
function syncChips() {
  $$("#filters .chip").forEach(c => c.classList.toggle("on", state.filters.has(c.dataset.f)));
  $("#clear-filters").hidden = state.filters.size === 0;
}
$("#filters").addEventListener("click", e => {
  const chip = e.target.closest(".chip");
  if (!chip) return;
  const f = chip.dataset.f;
  state.filters.has(f) ? state.filters.delete(f) : state.filters.add(f);
  store.set("hc.filters", [...state.filters]);
  syncChips();
  render();
});
$("#clear-filters").addEventListener("click", () => {
  state.filters.clear();
  store.set("hc.filters", []);
  syncChips();
  render();
});

// ── Spiele ─────────────────────────────────────────────────
const gamesEl = $("#games");
const tmpl = $("#game-template");

function skeleton() {
  gamesEl.innerHTML = "";
  for (let i = 0; i < 4; i++) {
    const s = document.createElement("div");
    s.className = "skeleton";
    gamesEl.appendChild(s);
  }
}

async function loadGames() {
  const id = ++state.reqId;
  skeleton();
  try {
    const res = await apiFetch(`/api/games?date=${dateInput.value}`);
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || `${res.status} ${res.statusText}`);
    if (id !== state.reqId) return;
    state.games = data.games || [];
    render();
  } catch (e) {
    if (id !== state.reqId) return;
    gamesEl.innerHTML = "";
    const box = document.createElement("div");
    box.className = "error";
    box.innerHTML = `<strong>Konnte Spiele nicht laden</strong><span></span><br><button class="btn">Nochmal versuchen</button>`;
    box.querySelector("span").textContent = e.message;
    box.querySelector("button").onclick = loadGames;
    gamesEl.appendChild(box);
  }
}

function emptyBox(title, text) {
  const box = document.createElement("div");
  box.className = "empty";
  box.innerHTML = "<strong></strong><span></span>";
  box.querySelector("strong").textContent = title;
  box.querySelector("span").textContent = text;
  return box;
}

function metaType(g) {
  if (g.round) return g.round;
  if (/playoff/i.test(g.seasonType)) return "Playoffs";
  if (/play-?in/i.test(g.seasonType)) return "Play-In";
  if (/pre/i.test(g.seasonType)) return "Preseason";
  if (/cup/i.test(g.subtype)) return "NBA Cup";
  return g.seasonType || "";
}

function render() {
  gamesEl.innerHTML = "";
  const active = [...state.filters];
  const list = active.length
    ? state.games.filter(g => active.some(f => g.badges.includes(f)))
    : state.games.slice();
  // Wer "Sehenswert" bewusst einschaltet, bekommt die besten Spiele zuerst
  if (state.filters.has("watch")) list.sort((a, b) => (b.watchScore ?? -1) - (a.watchScore ?? -1));

  if (!state.games.length) {
    gamesEl.appendChild(emptyBox("Keine Spiele an diesem Tag", "Probier einen anderen Tag – mit ← / → blätterst du."));
    return;
  }
  if (!list.length) {
    gamesEl.appendChild(emptyBox("Kein Spiel passt zu deinen Filtern", `${state.games.length} Spiele an diesem Tag – nimm einen Filter raus.`));
    return;
  }

  const target = state.newTab ? "_blank" : "_self";

  list.forEach(g => {
    const node = tmpl.content.cloneNode(true);
    const card = $(".card", node);

    for (const side of ["away", "home"]) {
      const t = g[side];
      const el = $(`.team.${side}`, node);
      const img = $(".logo", el);
      img.src = t.logo;
      img.alt = t.name;
      img.onerror = () => { img.style.visibility = "hidden"; };
      $(".tricode", el).textContent = t.tricode;
      $(".tname", el).textContent = t.name;
    }

    $(".meta-type", node).textContent = metaType(g);
    $(".meta-series", node).textContent = g.series || "";

    const shown = state.showBadges ? g.badges : g.badges.filter(b => state.filters.has(b));
    const bc = $(".badges", node);
    shown.forEach(b => {
      const s = document.createElement("span");
      s.className = `badge ${b}`;
      s.textContent = LABELS[b] || b;
      if (b === "watch" && g.watchScore != null) {
        s.textContent += ` · ${g.watchScore}`;
        if (g.watchScore >= 85) s.classList.add("must");
        s.title = g.watchScore >= 85 ? "Muss man sehen" : "Sehenswert";
      }
      bc.appendChild(s);
    });

    const play = $(".btn-play", node);
    const label = $(".btn-label", node);
    const ghost = $(".btn-ghost", node);
    ghost.href = g.gameUrl;
    ghost.target = target;
    play.target = target;

    if (g.allPossessionsUrl && state.playTarget === "app") {
      play.href = g.appUrl;
      label.textContent = "In NBA-App öffnen";
      play.title = "Öffnet das Spiel in der NBA-App – dort „All Possessions“ wählen";
    } else if (g.allPossessionsUrl) {
      // Über die eigene Zwischenseite: so öffnet iOS nicht die NBA-App, sondern Safari
      // (das Browser-Skript erkennt die nba.com-Seite dann am streamId-Parameter)
      play.href = "/go?to=" + encodeURIComponent(g.allPossessionsUrl);
      label.textContent = "All Possessions";
    } else if (g.status === "final") {
      play.href = g.watchUrl;
      play.classList.add("off");
      label.textContent = "Recap noch nicht da";
      play.title = "All Possessions erscheint meist einige Stunden nach Spielende";
    } else if (g.status === "live") {
      play.href = g.watchUrl;
      play.classList.add("off");
      label.textContent = "Läuft gerade";
    } else {
      play.href = g.gameUrl;
      play.classList.add("off");
      card.classList.add("dim");
      const t = g.tipoffUTC ? new Date(g.tipoffUTC) : null;
      label.textContent = t ? `Tip-off ${t.toLocaleString("de-AT", { weekday: "short", hour: "2-digit", minute: "2-digit" })}` : "Noch nicht gespielt";
    }

    gamesEl.appendChild(node);
  });
}

// ── Einstellungen ──────────────────────────────────────────
const dlg = $("#settings");
const emailIn = $("#nba-email");
const pwIn = $("#nba-password");
const statusEl = $("#account-status");

function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => (t.hidden = true), 2600);
}

function showStatus(check) {
  if (!check) { statusEl.hidden = true; return; }
  statusEl.hidden = false;
  statusEl.className = `status ${check.ok ? "ok" : "bad"}`;
  let txt = check.message || (check.ok ? "Login ok" : "Login fehlgeschlagen");
  if (check.name) txt = `${check.name}: ${txt}`;
  if (check.checkedAt) txt += ` · ${new Date(check.checkedAt * 1000).toLocaleString("de-AT", { dateStyle: "short", timeStyle: "short" })}`;
  statusEl.textContent = txt;
}

function applySettings(s) {
  emailIn.value = s.nbaEmail || "";
  pwIn.value = "";
  pwIn.placeholder = s.hasPassword ? "gespeichert – leer lassen zum Behalten" : "••••••••";
  $("#pw-hint").textContent = s.hasPassword ? "Ein Passwort ist gespeichert." : "";
  $("#delete-account").hidden = !(s.nbaEmail || s.hasPassword);
  showStatus(s.lastCheck);
}

async function api(method, url, body) {
  const res = await apiFetch(url, {
    method,
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `${res.status}`);
  return data;
}

$("#open-settings").addEventListener("click", async () => {
  $("#opt-show-badges").checked = state.showBadges;
  $("#opt-new-tab").checked = state.newTab;
  dlg.showModal();
  try { applySettings(await api("GET", "/api/settings")); }
  catch (e) { toast(`Einstellungen nicht ladbar: ${e.message}`); }
  if ($("#admin-section")) loadAdmin();
});

// ── Konto ──────────────────────────────────────────────────
async function logout() {
  try { await fetch("/api/auth/logout", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" }); }
  finally { location.replace("/login"); }
}
$("#logout").addEventListener("click", logout);
$("#logout-2").addEventListener("click", logout);

$("#pw-form").addEventListener("submit", async e => {
  e.preventDefault();
  try {
    await api("POST", "/api/me/password", { oldPassword: $("#old-pw").value, newPassword: $("#new-pw").value });
    $("#old-pw").value = $("#new-pw").value = "";
    toast("Passwort geändert – andere Geräte abgemeldet, Browser-Skripte gesperrt (neu installieren)");
  } catch (err) { toast(err.message); }
});

// ── Benutzerverwaltung (nur Admins) ────────────────────────
function renderAdmin(data) {
  $("#opt-signup").checked = data.signupOpen;
  const ul = $("#user-list");
  ul.innerHTML = "";
  data.users.forEach(u => {
    const li = document.createElement("li");
    li.innerHTML = `<span class="who"></span><span class="tags"></span>`;
    $(".who", li).textContent = u.username + (u.id === data.me ? " (du)" : "");
    const tags = $(".tags", li);
    if (u.isAdmin) tags.insertAdjacentHTML("beforeend", `<span class="tag admin">Admin</span> `);
    if (u.hasNba) tags.insertAdjacentHTML("beforeend", `<span class="tag">NBA</span>`);
    if (u.id !== data.me) {
      const mk = document.createElement("button");
      mk.type = "button";
      mk.className = "mini";
      mk.textContent = u.isAdmin ? "Admin entziehen" : "Zum Admin";
      mk.onclick = async () => {
        try { renderAdmin(await api("POST", `/api/admin/users/${u.id}/admin`, { isAdmin: !u.isAdmin })); }
        catch (err) { toast(err.message); }
      };
      const del = document.createElement("button");
      del.type = "button";
      del.className = "mini danger";
      del.textContent = "Löschen";
      del.onclick = async () => {
        if (!confirm(`Benutzer „${u.username}“ inkl. NBA-Zugangsdaten löschen?`)) return;
        try { renderAdmin(await api("DELETE", `/api/admin/users/${u.id}`)); toast("Benutzer gelöscht"); }
        catch (err) { toast(err.message); }
      };
      li.append(mk, del);
    }
    ul.appendChild(li);
  });
}
async function loadAdmin() {
  try { renderAdmin(await api("GET", "/api/admin/users")); }
  catch (err) { toast(err.message); }
}
if ($("#admin-section")) {
  $("#opt-signup").addEventListener("change", async e => {
    try { renderAdmin(await api("POST", "/api/admin/signup", { open: e.target.checked })); }
    catch (err) { toast(err.message); }
  });
  $("#new-user-form").addEventListener("submit", async e => {
    e.preventDefault();
    try {
      renderAdmin(await api("POST", "/api/admin/users", { username: $("#new-username").value.trim(), password: $("#new-password").value }));
      $("#new-username").value = $("#new-password").value = "";
      toast("Benutzer angelegt");
    } catch (err) { toast(err.message); }
  });
}
dlg.addEventListener("click", e => { if (e.target === dlg) dlg.close(); });

$("#account-form").addEventListener("submit", async e => {
  e.preventDefault();
  const btn = e.submitter || $("#account-form .primary");
  btn.disabled = true;
  try {
    applySettings(await api("POST", "/api/settings", { nbaEmail: emailIn.value, nbaPassword: pwIn.value || null }));
    toast("Gespeichert");
  } catch (err) { toast(`Fehler: ${err.message}`); }
  finally { btn.disabled = false; }
});

$("#check-login").addEventListener("click", async e => {
  const btn = e.currentTarget;
  btn.disabled = true;
  btn.textContent = "Prüfe …";
  try {
    if (pwIn.value || emailIn.value) {
      applySettings(await api("POST", "/api/settings", { nbaEmail: emailIn.value, nbaPassword: pwIn.value || null }));
    }
    showStatus(await api("POST", "/api/settings/check"));
  } catch (err) { toast(`Fehler: ${err.message}`); }
  finally { btn.disabled = false; btn.textContent = "Login testen"; }
});

$("#delete-account").addEventListener("click", async () => {
  if (!confirm("NBA-Zugangsdaten vom Server löschen?")) return;
  try { applySettings(await api("DELETE", "/api/settings")); toast("Zugangsdaten gelöscht"); }
  catch (err) { toast(`Fehler: ${err.message}`); }
});

$("#toggle-pw").addEventListener("click", () => {
  pwIn.type = pwIn.type === "password" ? "text" : "password";
});

$("#opt-show-badges").addEventListener("change", e => {
  state.showBadges = e.target.checked;
  store.set("hc.showBadges", state.showBadges);
  render();
});
$("#opt-new-tab").addEventListener("change", e => {
  state.newTab = e.target.checked;
  store.set("hc.newTab", state.newTab);
  render();
});

// ── Start ──────────────────────────────────────────────────
syncChips();
setPlayTarget(state.playTarget, false);
(() => {
  const q = new URLSearchParams(location.search).get("date");
  if (q && /^\d{4}-\d{2}-\d{2}$/.test(q)) return setDate(q, false);
  const d = new Date();
  // Europa: "letzte Nacht" = US-Spieltag von gestern
  d.setDate(d.getDate() - 1);
  setDate(iso(d), false);
})();
