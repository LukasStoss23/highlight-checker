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

const store = {
  get(k, d) { try { const v = localStorage.getItem(k); return v === null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch {} },
};

const state = {
  games: [],
  filters: new Set(store.get("hc.filters", [])),
  showBadges: store.get("hc.showBadges", false),
  newTab: store.get("hc.newTab", true),
  reqId: 0,
};

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
    const res = await fetch(`/api/games?date=${dateInput.value}`);
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

    if (g.allPossessionsUrl) {
      play.href = g.allPossessionsUrl;
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
  const res = await fetch(url, {
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
});
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
(() => {
  const q = new URLSearchParams(location.search).get("date");
  if (q && /^\d{4}-\d{2}-\d{2}$/.test(q)) return setDate(q, false);
  const d = new Date();
  // Europa: "letzte Nacht" = US-Spieltag von gestern
  d.setDate(d.getDate() - 1);
  setDate(iso(d), false);
})();
