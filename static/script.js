const $ = (s, ctx = document) => ctx.querySelector(s);
const filtersEl = $("#filters");
const dateInput = $("#date");
const prevBtn    = $("#prev");
const nextBtn    = $("#next");
const loadBtn    = $("#load");
const gamesEl    = $("#games");
const tmpl       = $("#game-template");

const labels = {
  pts30:       "30+ Pts",
  pts40:       "40+ Pts",
  pts50:       "50+ Pts",
  tripleDouble:"Triple‑Double",
  close4:      "Knapp Q4",
  closeGame:   "Knapp",
  overtime:    "OT"
};

let allGames = [];

// Datum initial auf gestern
(function(){
  const d = new Date();
  d.setDate(d.getDate() - 1);
  dateInput.valueAsDate = d;
})();

prevBtn.addEventListener("click", () => {
  const d = new Date(dateInput.value);
  d.setDate(d.getDate() - 1);
  dateInput.valueAsDate = d;
  loadGames();
});
nextBtn.addEventListener("click", () => {
  const d = new Date(dateInput.value);
  d.setDate(d.getDate() + 1);
  dateInput.valueAsDate = d;
  loadGames();
});
loadBtn.addEventListener("click", loadGames);
filtersEl.addEventListener("change", renderGames);
window.addEventListener("DOMContentLoaded", loadGames);

async function loadGames() {
  gamesEl.textContent = "⏳ Lädt …";
  try {
    const res = await fetch(`/api/games?date=${dateInput.value}`);
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
    const { games } = await res.json();
    allGames = games;
    renderGames();
  } catch (e) {
    gamesEl.textContent = `Fehler: ${e.message}`;
  }
}

function renderGames() {
  const checked = Array.from(filtersEl.querySelectorAll("input:checked"))
                       .map(i => i.value);

  const toShow = checked.length
    ? allGames.filter(g => checked.some(f => g.badges.includes(f)))
    : allGames;

  gamesEl.innerHTML = "";
  if (!toShow.length) {
    gamesEl.textContent = "Keine Spiele gefunden.";
    return;
  }

  toShow.forEach(g => {
    const node = tmpl.content.cloneNode(true);
    const link = node.querySelector(".game");

    // Use scraped replayLink if available, sonst Fallback
    link.href   = g.replayLink || `https://watchreplay.net/nba/game/${g.gameId}`;
    link.target = "_blank";

    $(".logo.away", node).src           = g.awayLogo;
    $(".name.away", node).textContent  = g.away;
    $(".logo.home", node).src          = g.homeLogo;
    $(".name.home", node).textContent  = g.home;
    $(".round", node).textContent      = g.round + " " + g.gameNum;
    $(".game-type", node).textContent  = g.gameType;
    $(".series-text", node).textContent = g.seriesPre || "";

    const bc = $(".badges", node);
    if (checked.length) {
      checked.forEach(f => {
        if (g.badges.includes(f)) {
          const span = document.createElement("span");
          span.className = `badge ${f}`;
          span.textContent = labels[f];
          bc.appendChild(span);
        }
      });
    }

    gamesEl.appendChild(node);
  });
}
