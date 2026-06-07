/**
 * Renders prompt timeline JSONL snapshots as a JMTY-history-style report.
 * The script groups prompt and summary events, builds a side TOC, and keeps
 * search/filter interactions entirely client-side for static hosting.
 */
(function () {
  const state = {
    events: Array.isArray(window.PROMPT_TIMELINE_EVENTS) ? window.PROMPT_TIMELINE_EVENTS : [],
    filter: "all",
    query: "",
    visibleLimit: 120,
  };

  const INITIAL_VISIBLE_LIMIT = 120;
  const LOAD_MORE_STEP = 120;
  const MAX_TOC_ITEMS = 260;
  const GROUP_WINDOW_MS = 3 * 60 * 1000;

  const nodes = {
    repoLabel: document.querySelector("#repoLabel"),
    statTotal: document.querySelector("#statTotal"),
    statSummarized: document.querySelector("#statSummarized"),
    statFirst: document.querySelector("#statFirst"),
    statLatest: document.querySelector("#statLatest"),
    searchInput: document.querySelector("#searchInput"),
    filters: Array.from(document.querySelectorAll("[data-filter]")),
    tocList: document.querySelector("#tocList"),
    phaseList: document.querySelector("#phaseList"),
    categoryStrip: document.querySelector("#categoryStrip"),
    timelineList: document.querySelector("#timelineList"),
    resultCount: document.querySelector("#resultCount"),
  };

  function normalizeEvents(events) {
    const prompts = [];
    const byId = new Map();

    events
      .filter((event) => event && event.kind === "prompt")
      .forEach((event) => {
        const normalized = {
          id: String(event.id || ""),
          timestamp: event.timestamp || "",
          timestampJst: event.timestamp_jst || event.timestamp || "",
          source: event.source || "agent",
          prompt: event.prompt_original || event.prompt_preview || "",
          promptPreview: event.prompt_preview || event.prompt_original || "",
          summary: event.summary || "",
          actions: Array.isArray(event.actions) ? event.actions.slice() : [],
          tags: Array.isArray(event.tags) ? event.tags.slice() : [],
          meta: event.meta || {},
        };
        if (normalized.id) {
          prompts.push(normalized);
          byId.set(normalized.id, normalized);
        }
      });

    events
      .filter((event) => event && event.kind === "summary" && event.parent_id)
      .forEach((event) => {
        const target = byId.get(String(event.parent_id));
        if (!target) return;
        if (event.summary) target.summary = event.summary;
        if (Array.isArray(event.actions)) target.actions.push(...event.actions);
        if (Array.isArray(event.tags)) target.tags.push(...event.tags);
      });

    return groupPromptFlow(prompts.sort((a, b) => String(a.timestamp).localeCompare(String(b.timestamp))));
  }

  function escapeHtml(value) {
    return String(value || "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  function clip(value, length) {
    const normalized = String(value || "").replace(/\s+/g, " ").trim();
    if (normalized.length <= length) return normalized;
    return `${normalized.slice(0, length - 1)}…`;
  }

  function lineCount(value) {
    return String(value || "").split(/\r\n|\r|\n/).length;
  }

  function normalizedText(value) {
    return String(value || "").replace(/\s+/g, " ").trim();
  }

  function isShortFragment(value) {
    const text = normalizedText(value);
    if (!text) return true;
    if (text.length <= 18) return true;
    if (/^(ok|OK|はい|了解|承認|お願いします|お願い\s*します|実行していいよ|進めて|コミットOK)$/.test(text)) return true;
    if (/[はをでにへともがの]$/.test(text) && text.length <= 34) return true;
    return false;
  }

  function mergeUnique(values) {
    return Array.from(new Set(values.filter(Boolean)));
  }

  function canGroup(previousGroup, item) {
    if (!previousGroup) return false;
    const previousItems = previousGroup.groupItems || [previousGroup];
    const previous = previousItems[previousItems.length - 1];
    const previousTime = new Date(previous.timestamp).getTime();
    const currentTime = new Date(item.timestamp).getTime();
    if (Number.isNaN(previousTime) || Number.isNaN(currentTime)) return false;
    if (currentTime - previousTime < 0 || currentTime - previousTime > GROUP_WINDOW_MS) return false;
    return isShortFragment(item.prompt) || isShortFragment(previous.prompt);
  }

  function groupPromptFlow(prompts) {
    const grouped = [];
    prompts.forEach((item) => {
      const previousGroup = grouped[grouped.length - 1];
      if (canGroup(previousGroup, item)) {
        previousGroup.groupItems.push(item);
        rebuildGroup(previousGroup);
        return;
      }
      grouped.push({ ...item, groupItems: [item], groupCount: 1, rawPrompt: item.prompt });
    });
    return grouped;
  }

  function rebuildGroup(group) {
    const items = group.groupItems || [group];
    const fragments = items.map((item) => normalizedText(item.prompt)).filter(Boolean);
    const shortFragments = fragments.filter((text) => text.length <= 40);
    const base = fragments.find((text) => text.length > 40) || fragments[0] || "";
    const addOns = fragments.filter((text) => text !== base);
    const displayPrompt = addOns.length
      ? `統合プロンプト: ${base}${base.endsWith("。") ? "" : "。"} 追加指示: ${addOns.join(" / ")}`
      : `統合プロンプト: ${shortFragments.join(" / ")}`;
    group.id = items.map((item) => item.id).join("--");
    group.timestamp = items[0].timestamp;
    group.timestampJst = items[0].timestampJst;
    group.source = "grouped-flow";
    group.prompt = displayPrompt;
    group.promptPreview = displayPrompt;
    group.summary = `近い時刻に分かれて入力された ${items.length} 件の短い指示を、同じ作業の流れとして統合表示しています。主な目的は「${clip(base || displayPrompt, 72)}」です。`;
    group.actions = mergeUnique(items.flatMap((item) => item.actions || []));
    group.tags = mergeUnique(["grouped", ...items.flatMap((item) => item.tags || [])]);
    group.meta = items[0].meta || {};
    group.groupCount = items.length;
    group.rawPrompt = items
      .map((item, index) => `#${index + 1} ${formatDate(item.timestamp)}\n${item.prompt || "原文未記録"}`)
      .join("\n\n---\n\n");
  }

  function slugify(value) {
    return String(value || "entry")
      .toLowerCase()
      .replace(/[^\p{Letter}\p{Number}]+/gu, "-")
      .replace(/^-+|-+$/g, "")
      .slice(0, 90) || "entry";
  }

  function formatDate(value, mode) {
    if (!value) return "-";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return value;
    const options = mode === "day"
      ? { year: "numeric", month: "2-digit", day: "2-digit" }
      : { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" };
    return new Intl.DateTimeFormat("ja-JP", options).format(date);
  }

  function deriveRepoLabel(prompts) {
    const cwd = prompts.map((item) => item.meta && item.meta.cwd).find(Boolean);
    if (!cwd) return "Agent prompt history";
    const parts = String(cwd).split("/").filter(Boolean);
    return `${parts.at(-1) || "repository"} / prompt history`;
  }

  function getFilteredPrompts() {
    const query = state.query.trim().toLowerCase();
    return normalizeEvents(state.events).filter((item) => {
      const hasSummary = Boolean(item.summary || item.actions.length);
      if (state.filter === "summarized" && !hasSummary) return false;
      if (state.filter === "open" && hasSummary) return false;
      if (!query) return true;
      const rawGroupText = (item.groupItems || []).map((groupItem) => groupItem.prompt).join("\n");
      const haystack = [item.prompt, rawGroupText, item.summary, item.actions.join(" "), item.source, item.tags.join(" ")]
        .join("\n")
        .toLowerCase();
      return haystack.includes(query);
    });
  }

  function renderStats(allPrompts, prompts) {
    const summarized = allPrompts.filter((item) => item.summary || item.actions.length);
    nodes.repoLabel.textContent = deriveRepoLabel(allPrompts);
    nodes.statTotal.textContent = String(allPrompts.length);
    nodes.statSummarized.textContent = String(summarized.length);
    nodes.statFirst.textContent = allPrompts[0] ? formatDate(allPrompts[0].timestamp, "day") : "-";
    nodes.statLatest.textContent = allPrompts.at(-1) ? formatDate(allPrompts.at(-1).timestamp, "day") : "-";
    nodes.resultCount.textContent = `${prompts.length}件`;
  }

  function renderToc(prompts) {
    if (!prompts.length) {
      nodes.tocList.innerHTML = '<p class="empty-state">記録がありません。</p>';
      return;
    }
    const visiblePrompts = prompts.slice(0, MAX_TOC_ITEMS);
    const overflow = prompts.length > visiblePrompts.length
      ? `<p class="toc-overflow">ほか ${prompts.length - visiblePrompts.length}件は検索で絞り込めます。</p>`
      : "";
    nodes.tocList.innerHTML = visiblePrompts
      .map((item, index) => {
        const id = entryId(item, index);
        return `<a href="#${escapeHtml(id)}"><span>${escapeHtml(formatDate(item.timestamp))}</span>${escapeHtml(clip(item.promptPreview || item.prompt, 42))}</a>`;
      })
      .join("") + overflow;
  }

  function renderPhases(prompts) {
    const groups = new Map();
    prompts.forEach((item) => {
      const key = formatDate(item.timestamp, "day");
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(item);
    });
    const entries = Array.from(groups.entries()).slice(-4);
    if (!entries.length) {
      nodes.phaseList.innerHTML = '<div><span>00</span><strong>未記録</strong><p>まだ履歴がありません。</p></div>';
      return;
    }
    nodes.phaseList.innerHTML = entries
      .map(([day, items], index) => {
        const summarized = items.filter((item) => item.summary || item.actions.length).length;
        return `<div><span>${String(index + 1).padStart(2, "0")}</span><strong>${escapeHtml(day)}</strong><p>${items.length}件 / 要約${summarized}件</p></div>`;
      })
      .join("");
  }

  function renderCategories(prompts) {
    const counts = new Map();
    prompts.forEach((item) => {
      const tags = item.tags.length ? item.tags : [item.source || "agent"];
      tags.forEach((tag) => counts.set(tag, (counts.get(tag) || 0) + 1));
    });
    const items = Array.from(counts.entries())
      .sort((a, b) => b[1] - a[1])
      .slice(0, 10);
    nodes.categoryStrip.innerHTML = items.length
      ? items.map(([tag, count]) => `<span>${escapeHtml(tag)} ${count}</span>`).join("")
      : "<span>no-tags</span>";
  }

  function entryId(item, index) {
    return `timeline-${String(index + 1).padStart(2, "0")}-${slugify(item.id || item.promptPreview || item.prompt)}`;
  }

  function renderTimeline(prompts) {
    if (!prompts.length) {
      nodes.timelineList.innerHTML = '<p class="empty-state">条件に合う記録がありません。</p>';
      return;
    }
    const visiblePrompts = prompts.slice(0, state.visibleLimit);
    const hasMore = prompts.length > visiblePrompts.length;
    nodes.timelineList.innerHTML = visiblePrompts
      .map((item, index) => {
        const hasSummary = Boolean(item.summary || item.actions.length);
        const id = entryId(item, index);
        const topic = item.tags[0] || item.source || "agent";
        const promptText = item.rawPrompt || item.prompt || "原文未記録";
        const displayPromptText = item.prompt || promptText;
        const promptLines = lineCount(promptText);
        const promptChars = promptText.length;
        const isLongPrompt = promptChars > 1800 || promptLines > 24;
        const promptPreview = isLongPrompt ? clip(promptText, 1800) : promptText;
        const groupBadge = item.groupCount > 1 ? `<span class="status grouped">統合${item.groupCount}件</span>` : "";
        const actions = item.actions.length
          ? `<ul class="actions-list">${item.actions.map((action) => `<li>${escapeHtml(action)}</li>`).join("")}</ul>`
          : '<p class="empty-state">未記録</p>';
        const fullPrompt = isLongPrompt
          ? `<details class="prompt-details">
              <summary>全文を表示 (${promptChars.toLocaleString("ja-JP")}文字 / ${promptLines.toLocaleString("ja-JP")}行)</summary>
              <pre class="prompt-block full">${escapeHtml(promptText)}</pre>
            </details>`
          : "";
        return `
          <article class="timeline-card" id="${escapeHtml(id)}" data-date="${escapeHtml(formatDate(item.timestamp, "day"))}">
            <div class="timeline-index">${String(index + 1).padStart(2, "0")}</div>
            <div class="timeline-content">
              <div class="card-meta">
                <span class="topic">${escapeHtml(topic)}</span>
                <span class="source">${escapeHtml(item.source)}</span>
                <span class="status ${hasSummary ? "confirmed" : "partial"}">${hasSummary ? "要約済み" : "要約待ち"}</span>
                ${groupBadge}
              </div>
              <h3>${escapeHtml(formatDate(item.timestamp))}: ${escapeHtml(clip(item.promptPreview || item.prompt, 64))}</h3>
              <p class="entry-label">要約</p>
              ${item.summary ? `<div class="text-block"><p>${escapeHtml(item.summary)}</p></div>` : '<p class="empty-state">まだ要約がありません。</p>'}
              <p class="entry-label">${item.groupCount > 1 ? "統合プロンプト" : "原文プロンプト"}</p>
              ${item.groupCount > 1 ? `<div class="text-block normalized-prompt"><p>${escapeHtml(displayPromptText)}</p></div><p class="entry-label">統合元の原文</p>` : ""}
              <div class="prompt-panel">
                <div class="prompt-toolbar">
                  <span>${promptChars.toLocaleString("ja-JP")}文字</span>
                  <span>${promptLines.toLocaleString("ja-JP")}行</span>
                  ${item.groupCount > 1 ? `<span>統合元 ${item.groupCount.toLocaleString("ja-JP")}件</span>` : ""}
                </div>
                <pre class="prompt-block">${escapeHtml(promptPreview)}</pre>
                ${fullPrompt}
              </div>
              <p class="entry-label">根拠 / アクション</p>
              ${actions}
              <a class="back-to-top" href="#top">上へ戻る</a>
            </div>
          </article>
        `;
      })
      .join("") + (hasMore
        ? `<button class="load-more" type="button" data-load-more>さらに ${Math.min(LOAD_MORE_STEP, prompts.length - visiblePrompts.length).toLocaleString("ja-JP")}件表示</button>`
        : "");
  }

  function render() {
    const allPrompts = normalizeEvents(state.events);
    const prompts = getFilteredPrompts();
    renderStats(allPrompts, prompts);
    renderToc(prompts);
    renderPhases(allPrompts);
    renderCategories(allPrompts);
    renderTimeline(prompts);
  }

  nodes.searchInput.addEventListener("input", (event) => {
    state.query = event.target.value;
    state.visibleLimit = INITIAL_VISIBLE_LIMIT;
    render();
  });

  nodes.filters.forEach((button) => {
    button.addEventListener("click", () => {
      state.filter = button.getAttribute("data-filter") || "all";
      state.visibleLimit = INITIAL_VISIBLE_LIMIT;
      nodes.filters.forEach((item) => item.classList.toggle("is-active", item === button));
      render();
    });
  });

  nodes.timelineList.addEventListener("click", (event) => {
    const target = event.target;
    if (!(target instanceof Element) || !target.matches("[data-load-more]")) return;
    state.visibleLimit += LOAD_MORE_STEP;
    render();
  });

  render();
})();
