const form = document.querySelector("#form");
const live = document.querySelector("#live");
const reportEl = document.querySelector("#report");
const button = form.querySelector("button");
const SEV = { critical: "Критично", warning: "Важно", notice: "Заметка" };

let running = false;
let timer = null;
let current = null;
let severity = "all";
let pageQuery = "";

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[char]));
}

function linkish(value) {
  const safe = esc(value);
  if (/^https?:\/\//i.test(value || "")) {
    return `<a href="${safe}" target="_blank" rel="noopener noreferrer">${safe}</a>`;
  }
  return safe;
}

function seconds(ms) {
  return (Number(ms || 0) / 1000).toFixed(1) + " с";
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (running) return;
  const data = new FormData(form);
  running = true;
  button.disabled = true;
  button.textContent = "Идёт аудит…";
  reportEl.hidden = true;
  live.hidden = false;
  live.innerHTML = `<p class="phase">Открываю стартовую страницу</p>`;
  try {
    const response = await fetch("/api/audits", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        url: data.get("url"),
        max_pages: Number(data.get("max_pages")),
        max_depth: Number(data.get("max_depth")),
        respect_robots: data.get("robots") === "on",
      }),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Не удалось запустить аудит");
    poll(payload.id);
  } catch (error) {
    showError(error.message);
    stop();
  }
});

function poll(id) {
  timer = setTimeout(async () => {
    try {
      const response = await fetch("/api/audits/" + id);
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error || "Аудит потерялся");
      renderLive(payload.progress);
      if (payload.status === "done") {
        current = payload.report;
        severity = "all";
        pageQuery = "";
        renderReport();
        stop();
        return;
      }
      if (payload.status === "error") throw new Error(payload.error || "Аудит оборвался");
      poll(id);
    } catch (error) {
      showError(error.message);
      stop();
    }
  }, 700);
}

function stop() {
  running = false;
  button.disabled = false;
  button.textContent = "Проверить сайт";
  if (timer) clearTimeout(timer);
  timer = null;
}

function showError(message) {
  live.hidden = false;
  live.innerHTML = `<p class="error">${esc(message)}</p>`;
}

function renderLive(progress) {
  if (!progress) return;
  const limit = progress.limit || 1;
  const width = Math.min(100, Math.round((progress.pages / limit) * 100));
  const rows = (progress.log || []).map((line) => `<li>${esc(line)}</li>`).join("");
  live.innerHTML = `
    <p class="phase">${esc(progress.phase || "Идёт обход")}</p>
    <div class="bar"><i style="width:${width}%"></i></div>
    <p class="current">${esc(progress.current || "")} · страниц ${esc(progress.pages)} из ${esc(limit)}, в очереди ${esc(progress.queued)}</p>
    <ul class="log">${rows}</ul>`;
}

function renderReport() {
  if (!current) return;
  const summary = current.summary;
  const color = summary.score >= 85 ? "#1f6b45" : summary.score >= 65 ? "#9a5412" : "#8e342c";
  live.hidden = true;
  live.innerHTML = "";
  reportEl.hidden = false;
  reportEl.innerHTML = `
    <section class="summary">
      <div class="ring" style="--p:${summary.score};--c:${color}"><strong>${summary.score}</strong></div>
      <div>
        <p class="kicker">${esc(summary.host || "")} · ${esc(summary.checked_at || "")}</p>
        <h2>${esc(summary.label)}</h2>
        <p class="meta">${linkish(summary.final_url || summary.start_url)} · ${seconds(summary.elapsed_ms)} · средний ответ ${seconds(summary.avg_time_ms)}</p>
        <div class="tiles">
          <div class="tile"><b>${summary.pages}</b><span>страниц в обходе</span></div>
          <div class="tile"><b>${summary.indexable}</b><span>можно индексировать</span></div>
          <div class="tile"><b>${summary.critical}</b><span>критично</span></div>
          <div class="tile"><b>${summary.warnings}</b><span>важно</span></div>
          <div class="tile"><b>${summary.sitemap_count}</b><span>адресов в sitemap</span></div>
        </div>
        <div class="actions">
          ${summary.downloads && summary.downloads.xlsx ? `<a class="ghost" href="${esc(summary.downloads.xlsx)}">Таблица ошибок</a>` : ""}
          ${summary.downloads && summary.downloads.docx ? `<a class="ghost" href="${esc(summary.downloads.docx)}">Общее ТЗ</a>` : ""}
          <button type="button" class="ghost" id="download">Скачать JSON</button>
          <button type="button" class="ghost" id="print">Печать</button>
        </div>
      </div>
    </section>
    <section class="fixes">
      <h2>С чего начать</h2>
      ${current.top_fixes.length ? `<ol>${current.top_fixes.map((item) => `<li><b>${esc(item.title)}.</b> ${esc(item.fix)}</li>`).join("")}</ol>` : `<p>В обойдённых страницах автоматические проверки не нашли технических проблем.</p>`}
    </section>
    <section class="issues">
      <h2>Замечания</h2>
      <div class="filters">
        ${["all", "critical", "warning", "notice"].map((key) => {
          const label = key === "all" ? "Все" : SEV[key];
          return `<button type="button" class="ghost" data-sev="${key}" aria-pressed="${severity === key}">${label}</button>`;
        }).join("")}
      </div>
      <div id="issue-list"></div>
    </section>
    <section class="table-wrap">
      <div class="tools">
        <h2>Страницы</h2>
        <input id="page-search" type="search" placeholder="Фильтр по адресу или title" value="${esc(pageQuery)}">
      </div>
      <table>
        <thead><tr><th>Адрес</th><th>Код</th><th>Индекс</th><th>Title</th><th>H1</th><th>Слова</th><th>Вход</th><th>мс</th></tr></thead>
        <tbody id="page-rows"></tbody>
      </table>
    </section>
    <ul class="limits">${summary.limitations.map((item) => `<li>${esc(item)}</li>`).join("")}</ul>`;

  document.querySelector("#download").addEventListener("click", download);
  document.querySelector("#print").addEventListener("click", () => window.print());
  reportEl.querySelectorAll("[data-sev]").forEach((node) => {
    node.addEventListener("click", () => {
      severity = node.dataset.sev;
      renderReport();
    });
  });
  const search = document.querySelector("#page-search");
  search.addEventListener("input", () => {
    pageQuery = search.value;
    renderPages();
  });
  renderIssues();
  renderPages();
}

function renderIssues() {
  const list = document.querySelector("#issue-list");
  const items = current.issues.filter((item) => severity === "all" || item.severity === severity);
  if (!items.length) {
    list.innerHTML = `<p>В этом фильтре пусто.</p>`;
    return;
  }
  list.innerHTML = items.map((item) => {
    const rows = item.examples && item.examples.length ? item.examples : item.urls;
    const extra = item.url_count > rows.length ? `<li>и ещё ${item.url_count - rows.length}</li>` : "";
    const links = rows.length
      ? `<ul class="urls">${rows.map((url) => `<li>${linkish(url)}</li>`).join("")}${extra}</ul>`
      : "";
    return `<article class="issue ${item.severity}">
      <h3><span class="badge">${SEV[item.severity]}</span>${esc(item.title)}</h3>
      <div class="cat">${esc(item.category)} · штраф ${item.penalty}</div>
      <p>${esc(item.why)}</p>
      <p><b>Что сделать.</b> ${esc(item.fix)}</p>
      ${links}
    </article>`;
  }).join("");
}

function renderPages() {
  const body = document.querySelector("#page-rows");
  const query = pageQuery.trim().toLowerCase();
  const rows = current.pages.filter((page) => {
    if (!query) return true;
    return (page.url + " " + page.title + " " + (page.h1 || []).join(" ")).toLowerCase().includes(query);
  });
  const shown = query ? rows.slice(0, 500) : rows.slice(0, 200);
  const hidden = rows.length - shown.length;
  body.innerHTML = shown.map((page) => {
    const codeClass = page.status === 200 ? "ok" : "bad";
    return `<tr>
      <td class="url">${linkish(page.url)}</td>
      <td class="${codeClass}">${page.status || "—"}</td>
      <td>${page.indexable ? "да" : esc(page.index_reason || "нет")}</td>
      <td class="clip" title="${esc(page.title)}">${esc(page.title || "—")}</td>
      <td class="clip" title="${esc((page.h1 || []).join(" | "))}">${esc((page.h1 || [])[0] || "—")}</td>
      <td>${page.words}</td>
      <td>${page.inlinks}</td>
      <td>${page.time_ms}</td>
    </tr>`;
  }).join("") || `<tr><td colspan="8">Ничего не нашлось.</td></tr>`;
  if (hidden > 0) {
    body.insertAdjacentHTML("beforeend", `<tr><td colspan="8">Ещё ${hidden} страниц в файле таблицы. На экране первые ${shown.length}.</td></tr>`);
  }
}

function download() {
  const blob = new Blob([JSON.stringify(current, null, 2)], { type: "application/json" });
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = "teh-audit.json";
  link.click();
  URL.revokeObjectURL(link.href);
}
