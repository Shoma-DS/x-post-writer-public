// X 下書きプレビュー：一覧表示・検索・投稿・削除・編集・ステータス管理を提供するフロントエンド。

const API = `${window.location.origin}/api`;

// ngrok経由アクセス時はブラウザ警告をスキップするヘッダーを付与
function apiFetch(url, options = {}) {
  const isNgrok = window.location.hostname.includes('ngrok');
  if (isNgrok) {
    options.headers = { 'ngrok-skip-browser-warning': '1', ...(options.headers || {}) };
  }
  return fetch(url, options);
}

async function readApiJson(res, label = 'API') {
  const text = await res.text();
  if (!text.trim()) {
    if (res.status === 404) {
      throw new Error(`${label}が見つかりません。プレビューサーバーを再起動してください`);
    }
    throw new Error(`${label}から空のレスポンスが返りました (HTTP ${res.status})`);
  }
  try {
    return JSON.parse(text);
  } catch (_) {
    throw new Error(`${label}からJSON以外のレスポンスが返りました (HTTP ${res.status})`);
  }
}

function materialIcon(name, options = {}) {
  const classes = ['material-symbols-rounded', 'ui-icon'];
  if (options.filled) classes.push('is-filled');
  if (options.className) classes.push(options.className);
  return `<span class="${classes.join(' ')}" aria-hidden="true">${esc(name)}</span>`;
}

let allDrafts    = [];   // 全下書きキャッシュ
let currentDraft = null; // 現在選択中の下書き
let publicUrl    = window.location.origin;
let searchQuery  = '';   // 検索クエリ
let pendingDeleteId = null; // 削除モーダル用
let imagePromptModalMode = 'generate';
let imageGenerationJob = null;
let imageGenerationJobId = null;
let imageGenerationTimer = null;
let imageGenerationDraftId = null;
let imageRewriteJob = null;
let imageRewriteTimer = null;
let imageRewriteDraftId = null;
let imageRewriteMode = 'rewrite';
let textRewriteTargetPartId = null;
let textRewriteMode = 'part';
let textRewriteBusy = false;
let autoPostJob = null;
let autoPostTimer = null;
let autoPostDraftId = null;
let pendingAutoPostAfterImage = null;
let shownAgentPromptNotices = new Set();
let lastObsidianSave = null;
let savedObsidianDraftIds = new Set();
let obsidianSaveByDraftId = new Map();
let accountPresets = [];
const ACCOUNT_ALL_KEY = 'all';
const ACCOUNT_ANONYMOUS_KEY = '__anonymous__';
const ANONYMOUS_DISPLAY_NAME = '匿名アカウント';
const ANONYMOUS_USERNAME = 'anonymous';
let selectedAccount = normalizeAccountKey(localStorage.getItem('x-preview-selected-account') || ACCOUNT_ALL_KEY);
let authState = { auth_enabled: false, user: null };
let draftTotal = 0;
let draftHasMore = false;
let draftOffset = 0;
let draftLoading = false;
let searchTimer = null;
let imageFilter = 'all'; // 'all', 'yes', 'no'
let logoDetectionByDraftId = new Map();
let logoDetectionLoadingDraftId = null;
let logoRegisterLoadingDraftId = null;
let logoManualTarget = null;
let logoShowHiddenDraftIds = new Set();
let activeView = localStorage.getItem('x-preview-active-view') || 'drafts';
let sourcePosts = [];
let currentSourcePost = null;
let sourceFacets = { authors: [], tags: [] };
let sourceActiveAuthor = '';
let sourceActiveTag = '';
let analyticsDashboard = null;
let analyticsLoading = false;

// グループ折りたたみ状態（未投稿: 開く / 投稿済み: 閉じる）
const groupCollapsed = { draft: false, published: true };

// 一覧ページング
const DRAFT_PAGE_SIZE = 20;

// 文字数チェックは課金/非課金アカウントに合わせて切り替える。
// 無料X通常ポスト換算: 全角140文字/半角280文字相当、URLは23カウント。
const X_FREE_ACCOUNT_UNIT_LIMIT = 280;
const X_URL_WEIGHT = 23;
const CHAR_CHECK_KEY = 'x-preview-char-count-check-enabled';
let charCountCheckEnabled = localStorage.getItem(CHAR_CHECK_KEY) === '1';
const DRAFT_SCORE_KEY_PREFIX = 'x-preview-draft-score:';

// ── 初期化 ──────────────────────────────────────────

async function init() {
  try {
    const res  = await apiFetch(`${API}/public-url`);
    const data = await res.json();
    publicUrl  = data.url || publicUrl;
  } catch (_) {}
  await loadAuthState();
  if (!authState.user) {
    renderLoginGate();
    return;
  }
  clearLoginGate();
  await loadAccounts();
  await setMainView(activeView, { initial: true });
}

async function loadAuthState() {
  try {
    const res = await apiFetch(`${API}/auth/me`);
    authState = await res.json();
  } catch (_) {
    authState = { auth_enabled: false, user: null };
  }
  renderAuthButton();
}

function renderAuthButton() {
  const btn = document.getElementById('auth-btn');
  const logoutBtn = document.getElementById('logout-btn');
  if (!btn) return;
  btn.innerHTML = '<span class="google-g" aria-hidden="true">G</span>';
  if (logoutBtn) logoutBtn.style.display = 'none';
  if (!authState.auth_enabled) {
    btn.title = 'Google OAuth 未設定';
    btn.classList.remove('active');
    return;
  }
  if (authState.user) {
    btn.title = `${authState.user.email || authState.user.display_name || 'ログイン中'}`;
    btn.classList.add('active');
    if (logoutBtn) {
      logoutBtn.style.display = 'inline-flex';
      logoutBtn.title = `${authState.user.email || authState.user.display_name || 'ログイン中'} からログアウト`;
    }
  } else {
    btn.title = 'Googleでログイン';
    btn.classList.remove('active');
  }
}

function handleAuthClick() {
  if (!authState.auth_enabled) {
    showToast('Google OAuth が未設定です', true);
    return;
  }
  if (authState.user) {
    showToast('ログイン済みです。ログアウトは隣のボタンから行えます');
    return;
  }
  window.location.href = '/auth/google/start';
}

function handleLogoutClick() {
  window.location.href = '/auth/logout';
}

function renderLoginGate() {
  document.body.classList.add('auth-required');
  const existing = document.getElementById('login-gate');
  if (existing) existing.remove();

  const gate = document.createElement('main');
  gate.id = 'login-gate';
  gate.className = 'login-gate';
  const canLogin = !!authState.auth_enabled;
  gate.innerHTML = `
    <section class="login-panel">
      <div class="login-mark">𝕏</div>
      <h1>X 下書きプレビュー</h1>
      <p>${canLogin ? 'Googleアカウントでログインしてください。' : 'Googleログイン設定がまだ有効ではありません。'}</p>
      <button class="google-login-btn" onclick="handleAuthClick()" ${canLogin ? '' : 'disabled'}>
        <span class="google-g">G</span>
        <strong>Googleでログイン</strong>
      </button>
    </section>`;
  document.body.appendChild(gate);
}

function clearLoginGate() {
  document.body.classList.remove('auth-required');
  document.getElementById('login-gate')?.remove();
}

async function loadAccounts() {
  try {
    const res = await apiFetch(`${API}/accounts`);
    const data = await res.json();
    accountPresets = Array.isArray(data.accounts) ? data.accounts : [];
  } catch (_) {
    accountPresets = [];
  }
  renderAccountSummary();
}

// ── 画面切り替え ────────────────────────────────────

async function setMainView(view, options = {}) {
  activeView = ['drafts', 'sources', 'analytics'].includes(view) ? view : 'drafts';
  localStorage.setItem('x-preview-active-view', activeView);
  document.querySelectorAll('.view-switch-btn').forEach(btn => btn.classList.remove('active'));
  document.getElementById(`view-${activeView}-btn`)?.classList.add('active');
  const search = document.getElementById('search-input');
  if (search) {
    search.placeholder = activeView === 'drafts'
      ? '下書きを検索...'
      : activeView === 'sources'
        ? '元投稿を検索...'
        : '分析対象を検索...';
  }
  document.getElementById('preview-area')?.classList.remove('is-posted-bg');

  if (activeView === 'drafts') {
    if (!allDrafts.length || options.initial) {
      await loadDraftList();
    } else {
      renderDraftList();
      if (currentDraft) renderPreview(currentDraft);
    }
    syncMobileTabForView();
    return;
  }

  if (activeView === 'sources') {
    await loadSourcePosts();
    syncMobileTabForView();
    return;
  }

  await loadAnalyticsDashboard();
  syncMobileTabForView();
}

async function refreshCurrentView() {
  if (activeView === 'sources') {
    await loadSourcePosts();
    return;
  }
  if (activeView === 'analytics') {
    await loadAnalyticsDashboard();
    return;
  }
  await loadDraftList();
}

function renderViewEmpty(title, detail = '') {
  const content = document.getElementById('preview-content');
  content.innerHTML = `
    <div class="empty-state">
      <div class="x-logo-large">𝕏</div>
      <p>${esc(title)}</p>
      <small>${esc(detail)}</small>
    </div>`;
}

// ── 下書きリスト読み込み ─────────────────────────────

async function loadDraftList({ append = false } = {}) {
  const el = document.getElementById('draft-list');
  if (draftLoading) return;
  draftLoading = true;
  if (!append) {
    draftOffset = 0;
    draftTotal = 0;
    draftHasMore = false;
    allDrafts = [];
    renderDraftLoading(0, '下書きを読み込み中...');
  } else {
    renderDraftList();
  }

  try {
    const params = new URLSearchParams({
      limit: String(DRAFT_PAGE_SIZE),
      offset: String(draftOffset),
    });
    if (isAccountFilterActive()) params.set('account', selectedAccount);
    if (searchQuery.trim()) params.set('q', searchQuery.trim());
    if (imageFilter !== 'all') params.set('image', imageFilter);

    const res = await apiFetch(`${API}/drafts?${params.toString()}`);
    const payload = await res.json();
    const items = Array.isArray(payload) ? payload : (payload.items || []);
    draftTotal = Array.isArray(payload) ? items.length : Number(payload.total || 0);
    draftHasMore = Array.isArray(payload) ? false : !!payload.has_more;
    draftOffset += items.length;
    allDrafts = append ? [...allDrafts, ...items] : items;

    if (!Array.isArray(allDrafts) || allDrafts.length === 0) {
      renderDraftList();
      return;
    }

    renderDraftList();

    // 現在の選択を維持 or 先頭を選択
    const targetId = allDrafts.some(d => d.draft_id === currentDraft?.draft_id)
      ? currentDraft.draft_id
      : allDrafts[0].draft_id;
    selectDraft(targetId);

  } catch (e) {
    el.innerHTML = `<div class="error-msg">取得エラー: ${e.message}</div>`;
  } finally {
    draftLoading = false;
    renderDraftList();
  }
}

function renderDraftLoading(progress = 0, label = '読み込み中...') {
  const el = document.getElementById('draft-list');
  const skeletons = Array.from({ length: 8 }, (_, i) => `
    <div class="draft-skeleton" style="--i:${i}">
      <div class="draft-skeleton-avatar"></div>
      <div class="draft-skeleton-lines">
        <div></div><div></div><div></div>
      </div>
    </div>`).join('');
  el.innerHTML = `
    <div class="list-loading-panel">
      <div class="list-loading-top">
        <span>${esc(label)}</span>
        <span>${Math.round(progress)}%</span>
      </div>
      <div class="list-progress"><div style="width:${Math.max(8, Math.min(100, progress))}%"></div></div>
    </div>
    ${skeletons}`;
}

// ── 下書きリストをフィルタして描画 ──────────────────

function renderDraftList() {
  if (activeView !== 'drafts') {
    if (activeView === 'sources') renderSourceList();
    return;
  }
  const el = document.getElementById('draft-list');
  const q  = searchQuery.trim().toLowerCase();

  const filteredBySearch = q
    ? allDrafts.filter(d => {
        const text = (d.display_name + ' @' + d.x_username + ' ' + (d.memo || '') +
          ' ' + (d.preview_content || '')).toLowerCase();
        return text.includes(q);
      })
    : allDrafts;

  let filtered = filteredBySearch;
  if (imageFilter === 'yes') {
    filtered = filtered.filter(d => d.has_image);
  } else if (imageFilter === 'no') {
    filtered = filtered.filter(d => !d.has_image);
  }

  const filtersHtml = `
    <div class="list-filters">
      <button id="filter-img-all" class="filter-btn ${imageFilter === 'all' ? 'active' : ''}" onclick="setImageFilter('all')">すべて</button>
      <button id="filter-img-yes" class="filter-btn ${imageFilter === 'yes' ? 'active' : ''}" onclick="setImageFilter('yes')">画像あり</button>
      <button id="filter-img-no" class="filter-btn ${imageFilter === 'no' ? 'active' : ''}" onclick="setImageFilter('no')">画像なし</button>
    </div>`;

  if (filtered.length === 0) {
    el.innerHTML = filtersHtml + '<div class="loading">該当なし</div>';
    return;
  }

  const pendingItems = filtered.filter(d => d.status !== 'published');
  const postedItems  = filtered.filter(d => d.status === 'published');

  const renderItem = d => {
    const partCount = Number(d.part_count || d.parts?.length || 1);
    const isThread  = partCount > 1;
    const isPosted  = d.status === 'published';
    const preview   = (d.preview_content || d.parts?.[0]?.content || '').slice(0, 48);
    const identity  = getDraftDisplayIdentity(d);
    const avatarHtml = renderIdentityAvatar(identity, 'avatar-sm');
    const threadBadge = isThread
      ? `<span class="badge badge-thread">ツリー ${partCount}</span>`
      : `<span class="badge badge-single">単発</span>`;
    const imageBadge = d.has_image
      ? `<span class="badge badge-image">画像付き</span>`
      : '';
    const obsidianBadge = d.obsidian_save?.saved
      ? `<span class="badge badge-obsidian">Obsidian保存済み</span>`
      : '';
    const qualityBadge = renderQualityBadge(assessDraftQuality(d));
    const scoreBadge = renderDraftScoreBadge(d);
    return `
      <div class="draft-item${isPosted ? ' posted' : ''}" data-id="${d.draft_id}"
           onclick="selectDraft('${d.draft_id}')">
        <div class="draft-item-top">
          ${avatarHtml}
          <div class="draft-item-account">
            ${esc(identity.displayName)} <span>${esc(identity.handle)}</span>
          </div>
          <button class="delete-btn" title="削除" aria-label="削除" onclick="confirmDelete(event,'${d.draft_id}')">${materialIcon('delete')}</button>
        </div>
        <div class="draft-item-preview">${esc(preview)}${preview.length >= 48 ? '…' : ''}</div>
        <div class="draft-item-meta">
          ${qualityBadge}
          ${scoreBadge}
          ${threadBadge}
          ${imageBadge}
          ${obsidianBadge}
          <span class="draft-item-date">${d.created_at}</span>
          ${d.memo ? `<span class="draft-item-date">・${esc(d.memo)}</span>` : ''}
        </div>
      </div>`;
  };

  const renderGroup = (key, label, items) => {
    if (items.length === 0) return '';
    const collapsed = groupCollapsed[key];
    const displayItems = items;
    return `
      <div class="group-header" onclick="toggleGroup('${key}')">
        <span class="group-chevron${collapsed ? '' : ' open'}">›</span>
        <span class="group-label">${label}</span>
        <span class="group-count">${items.length}</span>
      </div>
      <div class="group-body${collapsed ? ' collapsed' : ''}">
        ${displayItems.map(renderItem).join('')}
      </div>`;
  };

  const loaded = allDrafts.length;
  const total = Math.max(draftTotal, loaded);
  const progress = total ? Math.round((loaded / total) * 100) : 0;
  const loadMoreBtn = draftHasMore
    ? `<button class="load-more-btn" onclick="loadMoreDrafts()" ${draftLoading ? 'disabled' : ''}>
         ${draftLoading ? '読み込み中...' : `さらに表示 (${loaded}/${total})`}
       </button>`
    : `<div class="list-loaded-all">読み込み完了 (${loaded}/${total})</div>`;
  const progressBar = `
    <div class="list-loading-panel compact">
      <div class="list-loading-top">
        <span>${searchQuery.trim() ? '検索結果' : '下書き一覧'}</span>
        <span>${loaded}/${total} ・ ${progress}%</span>
      </div>
      <div class="list-progress"><div style="width:${Math.max(6, progress)}%"></div></div>
    </div>`;

  el.innerHTML =
    filtersHtml +
    progressBar +
    renderGroup('draft',  '未投稿', pendingItems) +
    renderGroup('published', '投稿済', postedItems) +
    loadMoreBtn;

  // アクティブ状態を再適用
  if (currentDraft) {
    document.querySelectorAll('.draft-item').forEach(el =>
      el.classList.toggle('active', el.dataset.id === currentDraft.draft_id)
    );
  }
}

// ── 投稿前品質チェック ───────────────────────────────

function getDraftPartsForQuality(draft) {
  if (Array.isArray(draft?.parts) && draft.parts.length > 0) {
    return draft.parts.map((part, index) => ({
      position: Number(part.position || index + 1),
      content: String(part.content || ''),
      image_url: part.image_url || '',
    }));
  }
  const preview = String(draft?.preview_content || '').trim();
  return preview ? [{ position: 1, content: preview, image_url: draft?.has_image ? 'attached' : '' }] : [];
}

function hasImagePromptForQuality(draft) {
  return Array.isArray(draft?.image_prompts)
    && draft.image_prompts.some(prompt => String(prompt?.prompt || '').trim());
}

const X_URL_PATTERN = /(?:https?:\/\/|www\.)[^\s<>"']+/gi;
const EMOJI_PATTERN = /\p{Extended_Pictographic}/u;
const graphemeSegmenter = typeof Intl !== 'undefined' && Intl.Segmenter
  ? new Intl.Segmenter('ja', { granularity: 'grapheme' })
  : null;

function textGraphemes(text) {
  const value = String(text || '');
  if (!value) return [];
  if (graphemeSegmenter) {
    return Array.from(graphemeSegmenter.segment(value), item => item.segment);
  }
  return Array.from(value);
}

function isFullWidthCodePoint(codePoint) {
  return (
    (codePoint >= 0x1100 && codePoint <= 0x11ff) ||
    (codePoint >= 0x2e80 && codePoint <= 0xa4cf) ||
    (codePoint >= 0xac00 && codePoint <= 0xd7af) ||
    (codePoint >= 0xf900 && codePoint <= 0xfaff) ||
    (codePoint >= 0xfe10 && codePoint <= 0xfe6f) ||
    (codePoint >= 0xff01 && codePoint <= 0xff60) ||
    (codePoint >= 0xffe0 && codePoint <= 0xffe6)
  );
}

function isFullWidthGrapheme(grapheme) {
  if (!grapheme || /\s/.test(grapheme) || EMOJI_PATTERN.test(grapheme)) return false;
  return Array.from(grapheme).some(char => isFullWidthCodePoint(char.codePointAt(0)));
}

function xFreeAccountLength(text) {
  const value = String(text || '');
  let units = 0;
  let rawChars = 0;
  let urlCount = 0;
  let cursor = 0;
  const matches = Array.from(value.matchAll(X_URL_PATTERN));

  for (const match of matches) {
    const url = match[0] || '';
    const index = match.index || 0;
    const before = value.slice(cursor, index);
    for (const grapheme of textGraphemes(before)) {
      units += isFullWidthGrapheme(grapheme) ? 2 : 1;
      rawChars += 1;
    }
    units += X_URL_WEIGHT;
    rawChars += textGraphemes(url).length;
    urlCount += 1;
    cursor = index + url.length;
  }

  const rest = value.slice(cursor);
  for (const grapheme of textGraphemes(rest)) {
    units += isFullWidthGrapheme(grapheme) ? 2 : 1;
    rawChars += 1;
  }
  return {
    units,
    rawChars,
    urlCount,
    limit: X_FREE_ACCOUNT_UNIT_LIMIT,
    over: units > X_FREE_ACCOUNT_UNIT_LIMIT,
  };
}

function repeatedTextSignals(parts) {
  const chunks = [];
  for (const part of parts) {
    const normalized = part.content
      .replace(/https?:\/\/\S+/g, '')
      .replace(/[！？!?。．\n]/g, '。')
      .split('。')
      .map(value => value.trim())
      .filter(value => value.length >= 10);
    chunks.push(...normalized);
  }
  const seen = new Set();
  const repeated = new Set();
  for (const chunk of chunks) {
    const key = chunk.replace(/\s+/g, '').toLowerCase();
    if (seen.has(key)) repeated.add(chunk);
    seen.add(key);
  }
  return [...repeated].slice(0, 3);
}

function countMatches(text, patterns) {
  return patterns.reduce((count, pattern) => count + (pattern.test(text) ? 1 : 0), 0);
}

function clampScore(value, max) {
  return Math.max(0, Math.min(max, value));
}

function knownToolSignals(text) {
  const known = text.match(/\b(?:ChatGPT|Claude|Codex|OpenAI|GitHub|Remotion|HyperFrames|Editframe|Threads|Instagram|Blender|Unity|Cursor|Hacker News)\b/g) || [];
  const latinBrands = text.match(/\b[A-Z][A-Za-z0-9]{2,}(?:[A-Z][A-Za-z0-9]+)?\b/g) || [];
  return Array.from(new Set([...known, ...latinBrands])).slice(0, 8);
}

function criterionRatio(item) {
  return Number(item?.max || 0) > 0 ? Number(item.score || 0) / Number(item.max || 20) : 0;
}

function scoreCriterionInsight(item) {
  const key = item?.key || '';
  const low = criterionRatio(item) < 0.8;
  const table = {
    hook: {
      good: '冒頭で疑問・数字・違和感を出せていて、スクロールを止める力があります。',
      improve: '1行目に「数字」「逆張り」「知らないと損」のどれかを明確に入れると、さらに止まりやすくなります。',
      rule: '投稿の1行目は、数字・逆張り・損失回避・原因指摘のいずれかを必ず入れ、読者が続きを見たくなる問いで始める。',
    },
    target: {
      good: '誰に向けた話かが読み取りやすく、読者が自分ごと化しやすい構成です。',
      improve: '初心者・副業層・AI量産中級者のどの層に一番刺す投稿なのかを、冒頭かPart 2でより明示すると強くなります。',
      rule: '投稿ごとに最重要ターゲットを1つ決め、冒頭またはPart 2で「誰のどんな悩みの話か」を明示する。',
    },
    painBenefit: {
      good: '課題と得られる変化がつながっていて、読む理由を作れています。',
      improve: '読者の痛みをもう一段具体化し、「それを放置すると何を損するか」まで書くと刺さりやすくなります。',
      rule: '本文では読者の痛み、放置した場合の損、得られる変化をセットで書く。',
    },
    specificity: {
      good: '固有名詞・数字・比較軸があり、情報としての信頼感があります。',
      improve: '数字、ツール名、使う場面、比較対象をもう1つ足すと、抽象論から実用情報に寄ります。',
      rule: '抽象的な主張で終わらせず、数字・固有名詞・具体的な使用場面・比較対象のうち最低2つを入れる。',
    },
    flow: {
      good: 'ツリーの流れが読みやすく、次の投稿へ進む理由が作れています。',
      improve: '各パートの最後に短い橋渡しを入れ、重複表現を削ると最後まで読まれやすくなります。',
      rule: 'ツリー投稿では各パート末尾に次を読みたくなる短い橋渡しを置き、同じ説明の繰り返しは削る。',
    },
    action: {
      good: '次に見る・試す・保存するなどの行動導線があります。',
      improve: '最後に「保存」「試す」「比較する」など、読後にやることを1つだけ明確にすると反応が取りやすくなります。',
      rule: '最終パートでは読者の次アクションを1つに絞り、保存・試す・比較・リプ確認など自然な導線で締める。',
    },
  };
  const fallback = {
    good: `${item?.label || '項目'}は一定の水準を満たしています。`,
    improve: `${item?.label || '項目'}をもう少し具体化すると投稿全体の説得力が上がります。`,
    rule: `${item?.label || '項目'}を次回以降の投稿でも具体的に確認する。`,
  };
  const insight = table[key] || fallback;
  return {
    id: key || String(item?.label || 'criterion').replace(/\s+/g, '-'),
    criterion_key: key,
    title: item?.label || '採点項目',
    score: Number(item?.score || 0),
    max: Number(item?.max || 20),
    detail: low ? insight.improve : insight.good,
    good: insight.good,
    improvement: insight.improve,
    rule: insight.rule,
  };
}

function buildScoreReport(criteria, totalScore) {
  const normalized = (Array.isArray(criteria) ? criteria : []).map(scoreCriterionInsight);
  const strengths = normalized
    .filter(item => item.max > 0 && item.score / item.max >= 0.8)
    .sort((a, b) => (b.score / b.max) - (a.score / a.max))
    .slice(0, 4);
  const weakItems = normalized
    .filter(item => item.max > 0 && item.score / item.max < 0.8)
    .sort((a, b) => (a.score / a.max) - (b.score / b.max));
  const improvementPoints = (weakItems.length ? weakItems : normalized.slice().sort((a, b) => (a.score / a.max) - (b.score / b.max)).slice(0, 3))
    .map((item, index) => ({
      id: item.id || `improvement-${index + 1}`,
      criterion_key: item.criterion_key,
      title: item.title,
      score: item.score,
      max: item.max,
      detail: item.improvement,
      rule: item.rule,
      selected: true,
    }));
  const fallbackStrengths = strengths.length ? strengths : normalized
    .slice()
    .sort((a, b) => (b.score / b.max) - (a.score / a.max))
    .slice(0, 2);
  const overview = totalScore >= 82
    ? '全体として投稿の引きが強く、読者が続きを読む理由も作れています。反応をさらに伸ばすなら、弱い項目だけを運用ルールへ戻すのが効率的です。'
    : totalScore >= 68
      ? '土台はできていますが、刺す対象・読後アクション・具体例のどれかを強めると反応が伸びやすい状態です。'
      : '現状は読み手の自分ごと化が弱くなりやすいので、フック・ターゲット・具体性を優先して改善したい状態です。';
  return {
    overview,
    strengths: fallbackStrengths.map(item => ({
      id: item.id,
      title: item.title,
      score: item.score,
      max: item.max,
      detail: item.good,
    })),
    improvement_points: improvementPoints,
  };
}

function scoreWithReport(score) {
  if (!score) return null;
  if (score.report?.improvement_points?.length) return score;
  return {
    ...score,
    report: buildScoreReport(score.criteria || [], Number(score.score || 0)),
  };
}

function scoreDraftForAudience(draft) {
  const parts = getDraftPartsForQuality(draft);
  const firstText = String(parts[0]?.content || '').trim();
  const fullText = parts.map(part => String(part.content || '')).join('\n');
  const normalized = fullText.replace(/\s+/g, ' ');
  const tools = knownToolSignals(fullText);
  const repeated = repeatedTextSignals(parts);

  const hookHits = countMatches(firstText, [
    /[？?]/,
    /(損|危険|注意|まだ|実は|知ら|なぜ|違う|遠回り|言い訳|比較|全部|まるっと)/,
    /(\d|万|円|%|倍|AI|API)/,
  ]);
  const targetHits = countMatches(normalized, [
    /(AI|SNS|X|投稿|運用|インスタ|Threads|マーケ|営業|制作|開発|個人|副業|経営|フリーランス)/,
    /(人|担当|社長|クリエイター|開発者|マーケター|店舗|チーム)/,
    /(伸びない|時間がない|毎回|手作業|課金|コスト|探す|作る|管理)/,
  ]);
  const painBenefitHits = countMatches(normalized, [
    /(損|悩|詰ま|面倒|消耗|遅い|高い|失敗|遠回り|課題|問題)/,
    /(任せ|自動|削減|増や|伸ば|差別化|時短|勝ち|資産|ラク|効率)/,
    /(だから|つまり|結論|要するに|ポイント|大事|先に)/,
  ]);
  const specificityHits = countMatches(normalized, [
    /(\d|万|円|%|倍|件|ステップ|つ|個)/,
    /(月額|買い切り|無料|有料|URL|ツール|API|ライブラリ|フレームワーク)/,
    new RegExp(tools.length ? tools.map(value => value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|') : '$^'),
  ]);
  const actionHits = countMatches(normalized, [
    /(次|まず|やる|使う|試す|保存|チェック|見る|置いて|リプ|詳しく|導線)/,
    /(ステップ|手順|方法|比較|リスト|テンプレ|URL)/,
  ]);
  const flowBase = parts.length >= 3 && parts.length <= 7 ? 10 : parts.length === 1 ? 5 : 8;
  const flowPenalty = repeated.length > 0 ? 4 : 0;
  const densityPenalty = parts.some(part => String(part.content || '').length > 700) ? 3 : 0;

  const criteria = [
    {
      key: 'hook',
      label: 'フック',
      score: clampScore(8 + hookHits * 4, 20),
      max: 20,
      detail: hookHits >= 2 ? '冒頭に疑問・違和感・数字があり止まりやすい' : '冒頭の引っかかりをもう少し強くできる',
    },
    {
      key: 'target',
      label: 'ターゲット適合',
      score: clampScore(6 + targetHits * 4, 20),
      max: 20,
      detail: targetHits >= 2 ? '誰の課題かが本文から読み取りやすい' : '誰向けの話かをもう一段具体化したい',
    },
    {
      key: 'painBenefit',
      label: '痛みと得',
      score: clampScore(6 + painBenefitHits * 4, 20),
      max: 20,
      detail: painBenefitHits >= 2 ? '課題と得られる変化がつながっている' : '悩みとベネフィットの接続を強めたい',
    },
    {
      key: 'specificity',
      label: '具体性',
      score: clampScore(6 + specificityHits * 4, 20),
      max: 20,
      detail: tools.length || specificityHits >= 2 ? `固有名詞・数字がある${tools.length ? `: ${tools.slice(0, 3).join(', ')}` : ''}` : '数字・固有名詞・比較軸を足すと信頼感が上がる',
    },
    {
      key: 'flow',
      label: '読み進めやすさ',
      score: clampScore(flowBase + 10 - flowPenalty - densityPenalty, 20),
      max: 20,
      detail: repeated.length ? '重複表現が少し目立つ' : '構成は読み進めやすい範囲',
    },
    {
      key: 'action',
      label: '行動導線',
      score: clampScore(5 + actionHits * 5, 20),
      max: 20,
      detail: actionHits ? '次に何を見る/試すかの導線がある' : '保存・確認・試すなどの次アクションが弱い',
    },
  ];
  const weightedTotal = Math.round(criteria.reduce((sum, item) => sum + item.score, 0) / criteria.reduce((sum, item) => sum + item.max, 0) * 100);
  const level = weightedTotal >= 82 ? 'ok' : weightedTotal >= 68 ? 'warn' : 'danger';
  const label = weightedTotal >= 82 ? '強い' : weightedTotal >= 68 ? '改善余地' : '弱い';
  return {
    score: weightedTotal,
    level,
    label,
    criteria,
    report: buildScoreReport(criteria, weightedTotal),
    scored_at: new Date().toISOString(),
    summary: `刺さり度 ${weightedTotal} / ${label}`,
  };
}

function draftScoreKey(draftId) {
  return `${DRAFT_SCORE_KEY_PREFIX}${draftId}`;
}

function loadDraftScore(draftId) {
  if (!draftId) return null;
  try {
    const raw = localStorage.getItem(draftScoreKey(draftId));
    return raw ? JSON.parse(raw) : null;
  } catch (_) {
    return null;
  }
}

function saveDraftScore(draftId, score) {
  if (!draftId || !score) return;
  localStorage.setItem(draftScoreKey(draftId), JSON.stringify(score));
}

function getSavedDraftScore(draft) {
  const draftId = draft?.draft_id;
  if (!draftId) return null;
  const stored = loadDraftScore(draftId);
  if (stored) return scoreWithReport(stored);
  if (draft.draft_score?.score) {
    const normalized = scoreWithReport(draft.draft_score);
    saveDraftScore(draftId, normalized);
    return normalized;
  }
  return null;
}

async function gradeCurrentDraft(event) {
  if (event) {
    event.preventDefault();
    event.stopPropagation();
  }
  if (!currentDraft) {
    showToast('下書きを選択してください', true);
    return;
  }
  const score = scoreDraftForAudience(currentDraft);
  let savedScore = score;
  let savedToServer = true;
  try {
    const res = await apiFetch(`${API}/draft/score`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ draft_id: currentDraft.draft_id, score }),
    });
    const data = await readApiJson(res, '採点保存');
    if (!res.ok || data.ok === false) throw new Error(data.error || '採点保存に失敗しました');
    savedScore = scoreWithReport(data.draft_score || score);
  } catch (error) {
    savedToServer = false;
  }
  savedScore = scoreWithReport(savedScore);
  currentDraft.draft_score = savedScore;
  const listDraft = allDrafts.find(item => item.draft_id === currentDraft.draft_id);
  if (listDraft) listDraft.draft_score = savedScore;
  saveDraftScore(currentDraft.draft_id, savedScore);
  showToast(savedToServer ? `✓ 採点しました: ${savedScore.score}` : `採点しました（この端末に保存）: ${savedScore.score}`, !savedToServer);
  renderDraftList();
  renderPreview(currentDraft);
}

async function applyScoreImprovements(event) {
  if (event) {
    event.preventDefault();
    event.stopPropagation();
  }
  if (!currentDraft) {
    showToast('下書きを選択してください', true);
    return;
  }
  const score = scoreWithReport(getSavedDraftScore(currentDraft));
  const points = score?.report?.improvement_points || [];
  if (!points.length) {
    showToast('先に採点してください', true);
    return;
  }
  const checkedIds = new Set(
    Array.from(document.querySelectorAll('.score-improvement-check:checked'))
      .map(input => input.value)
  );
  const selected = points.filter(item => checkedIds.has(String(item.id)));
  if (!selected.length) {
    showToast('取り入れる改善点を選択してください', true);
    return;
  }
  try {
    const res = await apiFetch(`${API}/draft/score/improvements`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        draft_id: currentDraft.draft_id,
        improvements: selected,
      }),
    });
    const data = await readApiJson(res, '改善点保存');
    if (!res.ok || data.ok === false) throw new Error(data.error || '改善点保存に失敗しました');
    const nextScore = scoreWithReport(data.draft_score || {
      ...score,
      applied_improvements: data.applied_improvements || selected,
    });
    currentDraft.draft_score = nextScore;
    const listDraft = allDrafts.find(item => item.draft_id === currentDraft.draft_id);
    if (listDraft) listDraft.draft_score = nextScore;
    saveDraftScore(currentDraft.draft_id, nextScore);
    showToast(`✓ 改善点を${selected.length}件、今後の運用に取り入れました`);
    renderDraftList();
    renderPreview(currentDraft);
  } catch (error) {
    showToast(`改善点保存失敗: ${error.message}`, true);
  }
}

function renderDraftScoreBadge(draft) {
  const score = getSavedDraftScore(draft);
  if (!score) return '';
  return `<span class="badge badge-score badge-score-${escAttr(score.level || 'warn')}" title="${escAttr(score.summary || '採点済み')}">刺さり ${Number(score.score || 0)}</span>`;
}

function renderDraftScoreDetails(score) {
  score = scoreWithReport(score);
  if (!score) {
    return `
      <div class="draft-score-panel draft-score-empty">
        <span>未採点</span>
        <button class="quality-toggle-btn" onclick="gradeCurrentDraft(event)">採点する</button>
      </div>`;
  }
  const report = score.report || {};
  const appliedIds = new Set((score.applied_improvements || []).map(item => String(item.id || item.criterion_key || item.rule || '')));
  const criteria = (score.criteria || []).map(item => `
    <div class="score-criterion score-criterion-${escAttr(score.level || 'warn')}">
      <div class="score-criterion-top">
        <strong>${esc(item.label)}</strong>
        <span>${Number(item.score || 0)} / ${Number(item.max || 20)}</span>
      </div>
      <p>${esc(item.detail || '')}</p>
    </div>`).join('');
  const strengths = (report.strengths || []).map(item => `
    <li><strong>${esc(item.title)}</strong><span>${Number(item.score || 0)} / ${Number(item.max || 20)}</span><p>${esc(item.detail || '')}</p></li>
  `).join('');
  const improvements = (report.improvement_points || []).map(item => {
    const id = String(item.id || item.criterion_key || item.title || '');
    const applied = appliedIds.has(id);
    return `
      <label class="score-improvement-item ${applied ? 'is-applied' : ''}">
        <input class="score-improvement-check" type="checkbox" value="${escAttr(id)}" ${applied ? '' : 'checked'}>
        <span>
          <strong>${esc(item.title || '改善点')}</strong>
          <small>${Number(item.score || 0)} / ${Number(item.max || 20)}</small>
          <p>${esc(item.detail || '')}</p>
          <em>${esc(item.rule || '')}</em>
          ${applied ? '<b>反映済み</b>' : ''}
        </span>
      </label>`;
  }).join('');
  return `
    <div class="draft-score-panel draft-score-${escAttr(score.level || 'warn')}">
      <div class="draft-score-head">
        <span>刺さり度 <strong>${Number(score.score || 0)}</strong></span>
        <button class="quality-toggle-btn" onclick="gradeCurrentDraft(event)">採点する</button>
      </div>
      <div class="score-criteria-grid">${criteria}</div>
      <details class="score-report-toggle" open>
        <summary>詳細レポート <span>良いところ・改善点</span></summary>
        <div class="score-report-body">
          <div class="score-report-overview">${esc(report.overview || '')}</div>
          <div class="score-report-section">
            <strong>良いところ</strong>
            <ul>${strengths || '<li><p>採点後に表示されます</p></li>'}</ul>
          </div>
          <div class="score-report-section">
            <strong>今後の改善点</strong>
            <div class="score-improvement-list">${improvements || '<p class="score-report-empty">改善点はありません</p>'}</div>
          </div>
          <button class="quality-toggle-btn score-apply-btn" onclick="applyScoreImprovements(event)">選択した改善点を今後の運用に取り入れる</button>
        </div>
      </details>
    </div>`;
}

function assessDraftQuality(draft) {
  const parts = getDraftPartsForQuality(draft);
  const checks = [];
  const hasImage = !!draft?.has_image || parts.some(part => !!part.image_url);
  const hasImagePrompt = hasImagePromptForQuality(draft);
  const original = draft?.original_tweet || {};
  const isThread = parts.length > 1 || Number(draft?.part_count || 0) > 1;
  const firstText = String(parts[0]?.content || '').trim();
  const fullText = parts.map(part => part.content).join('\n');

  const emptyParts = parts.filter(part => !part.content.trim());
  if (parts.length === 0 || emptyParts.length > 0) {
    checks.push({ level: 'danger', label: '空本文', detail: '本文が空のパーツがあります' });
  } else {
    checks.push({ level: 'ok', label: '本文', detail: `${parts.length}パーツ` });
  }

  if (charCountCheckEnabled) {
    const partLengths = parts.map(part => ({ ...part, xLength: xFreeAccountLength(part.content) }));
    const overLimitParts = partLengths.filter(part => part.xLength.over);
    if (overLimitParts.length > 0) {
      const detail = overLimitParts
        .map(part => `P${part.position} ${part.xLength.units}/${part.xLength.limit}`)
        .join(', ');
      checks.push({ level: 'danger', label: '文字数', detail: `無料X換算超過: ${detail}` });
    } else {
      const maxUnits = partLengths.reduce((max, part) => Math.max(max, part.xLength.units), 0);
      checks.push({ level: 'ok', label: '文字数', detail: `無料X換算 ${maxUnits}/${X_FREE_ACCOUNT_UNIT_LIMIT}（URL=23）` });
    }
  }

  if (hasImage) {
    checks.push({ level: 'ok', label: '画像', detail: '設定済み' });
  } else if (hasImagePrompt) {
    checks.push({ level: 'warn', label: '画像', detail: 'プロンプトあり・画像未設定' });
  } else {
    checks.push({ level: 'info', label: '画像', detail: '画像なし' });
  }

  if (isThread) {
    const veryShort = parts.filter(part => part.content.trim() && [...part.content.trim()].length < 18);
    if (veryShort.length > 0) {
      checks.push({ level: 'warn', label: 'ツリー', detail: `短すぎるパーツ: ${veryShort.map(part => part.position).join(', ')}` });
    } else {
      checks.push({ level: 'ok', label: 'ツリー', detail: `${parts.length || draft.part_count}パーツ` });
    }
  } else {
    checks.push({ level: 'ok', label: '形式', detail: '単発' });
  }

  if (original && Object.keys(original).length > 0) {
    if (original.tweet_url) {
      checks.push({ level: 'ok', label: '引用元', detail: 'URLあり' });
    } else {
      checks.push({ level: 'warn', label: '引用元', detail: '元投稿URLなし' });
    }
  }

  const repeated = repeatedTextSignals(parts);
  if (repeated.length > 0) {
    checks.push({ level: 'warn', label: '重複', detail: repeated[0] });
  } else if (parts.length > 0) {
    checks.push({ level: 'ok', label: '重複', detail: '目立つ重複なし' });
  }

  if (firstText && [...firstText].length < 18 && !/[？?！!]/.test(firstText)) {
    checks.push({ level: 'warn', label: 'フック', detail: '冒頭が弱い可能性' });
  }
  if (fullText && !/(https?:\/\/|詳しく|保存|試して|見て|チェック|どう思|ください|👇|↓)/.test(fullText)) {
    checks.push({ level: 'info', label: 'CTA', detail: '明確な誘導なし' });
  }

  const dangerCount = checks.filter(check => check.level === 'danger').length;
  const warnCount = checks.filter(check => check.level === 'warn').length;
  const infoCount = checks.filter(check => check.level === 'info').length;
  const score = Math.max(0, 100 - dangerCount * 35 - warnCount * 14 - infoCount * 4);
  const level = dangerCount > 0 || score < 60 ? 'danger' : warnCount > 0 || score < 82 ? 'warn' : 'ok';
  const problemCount = dangerCount + warnCount;
  const label = problemCount > 0 ? '要確認' : 'OK';
  const summary = problemCount > 0 ? `${problemCount}件確認` : '全部OK';
  return { level, label, score, checks, problemCount, summary };
}

function renderQualityBadge(quality) {
  const className = `badge-quality badge-quality-${quality.level}`;
  return `<span class="badge ${className}" title="${escAttr(quality.summary)}">${esc(quality.label)}${quality.problemCount ? ` ${quality.problemCount}` : ''}</span>`;
}

function renderQualityPanel(draft) {
  const quality = assessDraftQuality(draft);
  const draftScore = getSavedDraftScore(draft);
  const charToggleLabel = charCountCheckEnabled ? '文字数チェック ON' : '文字数チェック OFF';
  const items = quality.checks.map(check => `
    <div class="quality-check quality-check-${check.level}">
      <span class="quality-check-icon">${qualityIcon(check.level)}</span>
      <span class="quality-check-label">${esc(check.label)}</span>
      <span class="quality-check-detail">${esc(check.detail)}</span>
    </div>`).join('');
  return `
    <details class="quality-panel quality-panel-${quality.level}" aria-label="投稿前チェック">
      <summary class="quality-panel-head">
        <span class="quality-title">${materialIcon('fact_check', { filled: true })}投稿前チェック <strong>${esc(quality.label)}</strong><small>${esc(quality.summary)}</small></span>
        <span class="quality-panel-actions">
          ${draftScore ? `<span class="quality-score quality-score-${escAttr(draftScore.level || 'warn')}">刺さり ${Number(draftScore.score || 0)}</span>` : '<span class="quality-score">未採点</span>'}
          <button class="quality-toggle-btn" onclick="gradeCurrentDraft(event)">採点する</button>
          <button class="quality-toggle-btn ${charCountCheckEnabled ? 'active' : ''}" onclick="toggleCharCountCheck(event)">${esc(charToggleLabel)}</button>
        </span>
      </summary>
      <div class="quality-detail-body">
        <div class="quality-check-grid">${items}</div>
        ${renderDraftScoreDetails(draftScore)}
      </div>
    </details>`;
}

function toggleCharCountCheck(event) {
  if (event) {
    event.preventDefault();
    event.stopPropagation();
  }
  charCountCheckEnabled = !charCountCheckEnabled;
  localStorage.setItem(CHAR_CHECK_KEY, charCountCheckEnabled ? '1' : '0');
  renderDraftList();
  if (currentDraft) renderPreview(currentDraft);
}

function qualityIcon(level) {
  if (level === 'danger') return materialIcon('error', { filled: true });
  if (level === 'warn') return materialIcon('warning', { filled: true });
  if (level === 'ok') return materialIcon('check_circle', { filled: true });
  return materialIcon('info', { filled: true });
}

function toggleGroup(key) {
  groupCollapsed[key] = !groupCollapsed[key];
  renderDraftList();
}

function loadMoreDrafts() {
  if (!draftHasMore || draftLoading) return;
  loadDraftList({ append: true });
}

// ── 検索 ─────────────────────────────────────────────

function onSearch(value) {
  searchQuery = value;
  clearTimeout(searchTimer);
  if (activeView === 'drafts') {
    renderDraftLoading(20, '検索中...');
    searchTimer = setTimeout(() => loadDraftList(), 250);
  } else if (activeView === 'sources') {
    searchTimer = setTimeout(() => renderSourceList(), 120);
  } else {
    searchTimer = setTimeout(() => renderAnalyticsDashboard(), 120);
  }
}

function setImageFilter(val) {
  imageFilter = val;
  document.querySelectorAll('.filter-btn').forEach(btn => {
    btn.classList.toggle('active', btn.id === `filter-img-${val}`);
  });
  currentDraft = null;
  if (activeView === 'drafts') loadDraftList();
}

// ── 元投稿分析 ──────────────────────────────────────

function formatMetric(value) {
  const num = Number(value || 0);
  if (num >= 1000000) return `${(num / 1000000).toFixed(1)}M`;
  if (num >= 1000) return `${(num / 1000).toFixed(1)}K`;
  return num.toLocaleString();
}

async function loadSourcePosts() {
  const el = document.getElementById('draft-list');
  el.innerHTML = '<div class="loading">元投稿を読み込み中...</div>';
  renderViewEmpty('元投稿を選択してください', 'ブックマーク済み投稿を分析して、自分の投稿へ転用できます');
  try {
    const params = new URLSearchParams({ limit: '150' });
    if (searchQuery.trim()) params.set('q', searchQuery.trim());
    const res = await apiFetch(`${API}/source-posts?${params.toString()}`);
    const data = await readApiJson(res, '元投稿一覧');
    sourcePosts = data.items || [];
    sourceFacets = data.facets || { authors: [], tags: [] };
    renderSourceList();
    if (sourcePosts.length) selectSourcePost(sourcePosts[0].tweet_id, { skipTab: true });
  } catch (error) {
    el.innerHTML = `<div class="error-msg">元投稿取得エラー: ${esc(error.message)}</div>`;
  }
}

function sourceAuthorOptions() {
  const fromFacets = Array.isArray(sourceFacets.authors) ? sourceFacets.authors : [];
  if (fromFacets.length) return fromFacets;
  const counts = new Map();
  for (const item of sourcePosts) {
    const key = item.author_username || '';
    if (!key) continue;
    const current = counts.get(key) || { author_username: key, author_name: item.author_name || key, count: 0 };
    current.count += 1;
    counts.set(key, current);
  }
  return Array.from(counts.values()).sort((a, b) => b.count - a.count);
}

function sourceTagOptions() {
  const fromFacets = Array.isArray(sourceFacets.tags) ? sourceFacets.tags : [];
  if (fromFacets.length) return fromFacets;
  const counts = new Map();
  for (const item of sourcePosts) {
    for (const tag of item.tags || []) counts.set(tag, (counts.get(tag) || 0) + 1);
  }
  return Array.from(counts.entries()).map(([tag, count]) => ({ tag, count })).sort((a, b) => b.count - a.count);
}

function sourceMatchesFilters(item, query) {
  const searchable = `${item.text} ${item.author_username} ${item.author_name} ${(item.tags || []).join(' ')}`.toLowerCase();
  if (query && !searchable.includes(query)) return false;
  if (sourceActiveAuthor && String(item.author_username || '').toLowerCase() !== sourceActiveAuthor.toLowerCase()) return false;
  if (sourceActiveTag && !(item.tags || []).includes(sourceActiveTag)) return false;
  return true;
}

function renderSourceFilterPanel(filteredCount) {
  const authors = sourceAuthorOptions().slice(0, 12);
  const tags = sourceTagOptions().slice(0, 16);
  const hasFilter = !!(sourceActiveAuthor || sourceActiveTag || searchQuery.trim());
  return `
    <div class="source-filter-panel">
      <div class="source-filter-top">
        <strong>元投稿ライブラリ</strong>
        <span>${filteredCount} / ${sourcePosts.length}件</span>
        ${hasFilter ? `<button class="text-link-btn" onclick="clearSourceFilters()">解除</button>` : ''}
      </div>
      <div class="source-chip-row" aria-label="投稿者フィルタ">
        <button class="source-chip ${!sourceActiveAuthor ? 'active' : ''}" onclick="setSourceAuthorFilter('')">全アカウント</button>
        ${authors.map(author => `
          <button class="source-chip ${sourceActiveAuthor.toLowerCase() === String(author.author_username || '').toLowerCase() ? 'active' : ''}" onclick="setSourceAuthorFilter('${escAttr(author.author_username || '')}')">
            @${esc(author.author_username || '')}<span>${Number(author.count || 0)}</span>
          </button>`).join('')}
      </div>
      <div class="source-chip-row" aria-label="タグフィルタ">
        <button class="source-chip ${!sourceActiveTag ? 'active' : ''}" onclick="setSourceTagFilter('')">全タグ</button>
        ${tags.map(item => `
          <button class="source-chip ${sourceActiveTag === item.tag ? 'active' : ''}" onclick="setSourceTagFilter('${escAttr(item.tag || '')}')">
            ${esc(item.tag || '')}<span>${Number(item.count || 0)}</span>
          </button>`).join('')}
      </div>
    </div>`;
}

function setSourceAuthorFilter(author) {
  sourceActiveAuthor = author || '';
  renderSourceList();
}

function setSourceTagFilter(tag) {
  sourceActiveTag = tag || '';
  renderSourceList();
}

function clearSourceFilters() {
  sourceActiveAuthor = '';
  sourceActiveTag = '';
  searchQuery = '';
  const search = document.getElementById('search-input');
  if (search) search.value = '';
  renderSourceList();
}

function renderSourceList() {
  if (activeView !== 'sources') return;
  const el = document.getElementById('draft-list');
  const q = searchQuery.trim().toLowerCase();
  const filtered = sourcePosts.filter(item => sourceMatchesFilters(item, q));
  if (!filtered.length) {
    el.innerHTML = renderSourceFilterPanel(0) + '<div class="loading">条件に合う元投稿がありません</div>';
    return;
  }
  el.innerHTML = `
    ${renderSourceFilterPanel(filtered.length)}
    ${filtered.map(item => {
      const active = currentSourcePost?.tweet_id === item.tweet_id;
      const metrics = item.metrics || {};
      const preview = String(item.text || '').replace(/\s+/g, ' ').slice(0, 74);
      const tags = (item.tags || []).slice(0, 3).map(tag => `<span class="source-mini-tag">${esc(tag)}</span>`).join('');
      return `
        <div class="draft-item source-item ${active ? 'active' : ''}" data-id="${escAttr(item.tweet_id)}" onclick="selectSourcePost('${escAttr(item.tweet_id)}')">
          <div class="draft-item-top">
            <div class="avatar-sm">${materialIcon('travel_explore')}</div>
            <div class="draft-item-account">${esc(item.author_name || item.author_username)} <span>@${esc(item.author_username || '')}</span></div>
          </div>
          <div class="draft-item-preview">${esc(preview)}${preview.length >= 74 ? '…' : ''}</div>
          <div class="source-mini-tags">${tags}</div>
          <div class="draft-item-meta">
            ${item.draft_id ? '<span class="badge badge-quality-ok">下書き化済み</span>' : '<span class="badge badge-draft">未下書き</span>'}
            ${item.analysis ? '<span class="badge badge-score-ok">分析済み</span>' : ''}
            ${Number(item.thread_part_count || 0) > 1 ? `<span class="badge badge-score-warn">ツリー ${Number(item.thread_part_count)}</span>` : ''}
            <span class="draft-item-date">imp ${formatMetric(metrics.impressions)}</span>
            <span class="draft-item-date">like ${formatMetric(metrics.likes)}</span>
          </div>
        </div>`;
    }).join('')}`;
}

async function selectSourcePost(tweetId, options = {}) {
  const item = sourcePosts.find(post => String(post.tweet_id) === String(tweetId));
  if (!item) return;
  currentSourcePost = item;
  renderSourceList();
  renderSourcePost(item);
  if (!options.skipTab && window.innerWidth <= 640) switchTab('preview');
}

function sourcePostMediaHtml(item) {
  const threadMedia = (item.thread_parts || []).flatMap(part => Array.isArray(part.media) ? part.media : []);
  const media = [...(Array.isArray(item.media) ? item.media : []), ...threadMedia];
  const first = media.find(entry => entry.url || entry.preview_url);
  if (!first) return '';
  const src = normalizeImageUrl(first.preview_url || first.url || '');
  return src ? `<div class="source-media"><img src="${escAttr(src)}" alt="元投稿メディア" loading="lazy"></div>` : '';
}

function normalizeImageUrl(url) {
  if (!url) return '';
  if (url.startsWith('/local-image') || url.startsWith('/remote-image')) return url;
  if (/^https?:\/\//.test(url)) return `/remote-image?url=${encodeURIComponent(url)}`;
  return url;
}

function sourceTagRow(item) {
  const tags = item.tags || [];
  if (!tags.length) return '';
  return `<div class="source-tag-row">${tags.map(tag => `<span>${esc(tag)}</span>`).join('')}</div>`;
}

function sourcePostEmbedUrls(item) {
  const parts = Array.isArray(item.thread_parts) ? item.thread_parts : [];
  const urls = (parts.length > 1)
    ? parts.map(part => part.tweet_url).filter(Boolean)
    : [item.tweet_url || (parts[0] && parts[0].tweet_url)].filter(Boolean);
  return [...new Set(urls)];
}

function sourcePostEmbedHtml(item) {
  const urls = sourcePostEmbedUrls(item);
  if (!urls.length) {
    return '<div class="original-unavailable">元投稿URLが不明です</div>';
  }

  const stamp = Date.now();
  const slots = urls.map((url, i) => {
    const id = `source-oembed-slot-${stamp}-${i}`;
    requestAnimationFrame(() => loadOembed(url, id));
    const label = urls.length > 1 ? `Part ${i + 1} を読み込み中...` : '読み込み中...';
    return `<div class="oembed-slot source-oembed-slot" id="${id}">
      <div class="oembed-loading">${label}</div>
    </div>`;
  }).join('');

  return `<div class="source-embed-stack" aria-label="元投稿の埋め込み">${slots}</div>`;
}

function renderSourceThread(item) {
  const parts = Array.isArray(item.thread_parts) ? item.thread_parts : [];
  if (parts.length <= 1) {
    return `<p>${formatText(item.text || '')}</p>`;
  }
  return `
    <div class="source-thread-timeline" aria-label="元投稿ツリー">
      ${parts.map(part => {
        const metrics = part.metrics || {};
        return `
          <article class="source-thread-part">
            <div class="source-thread-index">${Number(part.position || 0)}</div>
            <div class="source-thread-body">
              <div class="source-thread-meta">
                <strong>Part ${Number(part.position || 0)}</strong>
                <span>imp ${formatMetric(metrics.impressions)} ・ like ${formatMetric(metrics.likes)}</span>
              </div>
              <p>${formatText(part.text || '')}</p>
            </div>
          </article>`;
      }).join('')}
    </div>`;
}

function renderSourcePost(item) {
  const content = document.getElementById('preview-content');
  const metrics = item.metrics || {};
  const analysis = item.analysis;
  const partCount = Number(item.thread_part_count || 0);
  content.innerHTML = `
    <div class="source-page">
      <div class="preview-header source-page-header">
        <div class="preview-header-info">
          <h3>${esc(item.author_name || item.author_username)} <span>@${esc(item.author_username || '')}</span></h3>
          <p>元投稿 ・ ${esc(item.created_at || '')}${partCount > 1 ? ` ・ ツリー ${partCount}パーツ` : ''}</p>
        </div>
        <div class="header-actions">
          <a class="image-source-copy-btn" href="${escAttr(item.tweet_url)}" target="_blank">${materialIcon('open_in_new')}Xで開く</a>
          ${item.draft_id ? `<button class="image-source-copy-btn" onclick="setMainView('drafts').then(() => selectDraft('${escAttr(item.draft_id)}'))">${materialIcon('edit_note')}下書きを開く</button>` : ''}
          <button class="post-btn" onclick="analyzeCurrentSourcePost()">${materialIcon('analytics', { filled: true })}分析する</button>
        </div>
      </div>
      <section class="source-text-panel source-embed-panel">
        ${sourceTagRow(item)}
        <div class="source-metrics-row">
          <span>imp <strong>${formatMetric(metrics.impressions)}</strong></span>
          <span>like <strong>${formatMetric(metrics.likes)}</strong></span>
          <span>rt <strong>${formatMetric(metrics.retweets)}</strong></span>
          <span>bm <strong>${formatMetric(metrics.bookmarks)}</strong></span>
          <span>ER <strong>${Number(item.engagement_rate || 0).toFixed(2)}%</strong></span>
        </div>
        ${sourcePostEmbedHtml(item)}
        <details class="source-text-fallback">
          <summary>${materialIcon('article')}取得本文を表示</summary>
          <div class="source-text-fallback-body">
            ${renderSourceThread(item)}
            ${sourcePostMediaHtml(item)}
          </div>
        </details>
      </section>
      <div id="source-analysis-slot">
        ${analysis ? renderSourceAnalysis(analysis) : '<div class="source-empty-report">分析すると、フック・構成・ツリーの流れ・その投稿の良さを詳細レポートにします。</div>'}
      </div>
    </div>`;
}

function renderSourceAnalysis(analysis) {
  const strengths = (analysis.strengths || []).map(item => `
    <li><strong>${esc(item.title)}</strong><p>${esc(item.detail)}</p></li>`).join('');
  const appliedPayload = loadSourceAppliedIds(analysis.tweet_id);
  const observedPatterns = (analysis.transfer_points || []).map(item => {
    const applied = appliedPayload.has(String(item.id));
    return `
      <label class="score-improvement-item ${applied ? 'is-applied' : ''}">
        <input class="source-transfer-check" type="checkbox" value="${escAttr(item.id)}" ${applied ? '' : 'checked'}>
        <span>
          <strong>${esc(item.title)}</strong>
          <p>${esc(item.detail)}</p>
          <em>${esc(item.rule)}</em>
          ${applied ? '<b>反映済み</b>' : ''}
        </span>
      </label>`;
  }).join('');
  const structure = analysis.structure || {};
  const thread = analysis.thread_analysis || {};
  const threadParts = Array.isArray(thread.parts) ? thread.parts : [];
  const threadHtml = threadParts.length ? `
    <div class="source-thread-analysis">
      <strong>ツリー構成の良さ</strong>
      <p>${esc(thread.summary || '')}</p>
      <div class="source-thread-role-list">
        ${threadParts.map(part => `
          <div class="source-thread-role">
            <span>Part ${Number(part.position || 0)} / ${esc(part.role || '-')}</span>
            <strong>${esc(part.good_point || '')}</strong>
            <p>${esc(part.excerpt || '')}</p>
          </div>`).join('')}
      </div>
    </div>` : '';
  const tags = (analysis.tags || []).map(tag => `<span>${esc(tag)}</span>`).join('');
  return `
    <details class="source-analysis-panel" open>
      <summary>元投稿の良さ分析 <span>強さ ${Number(analysis.score || 0)}</span></summary>
      <div class="source-analysis-body">
        ${tags ? `<div class="source-tag-row source-analysis-tags">${tags}</div>` : ''}
        <div class="score-report-overview">${esc(analysis.overview || '')}</div>
        <div class="source-analysis-grid">
          <div><span>フック型</span><strong>${esc(analysis.hook_type || '-')}</strong></div>
          <div><span>形式</span><strong>${structure.is_thread ? `ツリー ${Number(structure.thread_part_count || 0)}` : '単発'}</strong></div>
          <div><span>数字</span><strong>${structure.has_number ? 'あり' : 'なし'}</strong></div>
          <div><span>比較</span><strong>${structure.has_comparison ? 'あり' : 'なし'}</strong></div>
          <div><span>手順化</span><strong>${structure.has_steps ? 'あり' : 'なし'}</strong></div>
          <div><span>メディア</span><strong>${structure.has_media ? 'あり' : 'なし'}</strong></div>
          <div><span>CTA</span><strong>${structure.has_cta ? 'あり' : 'なし'}</strong></div>
        </div>
        <div class="score-report-section">
          <strong>この投稿そのものの良いところ</strong>
          <ul>${strengths}</ul>
        </div>
        ${threadHtml}
        <div class="score-report-section">
          <strong>採用したい良い型</strong>
          <div class="score-improvement-list">${observedPatterns}</div>
        </div>
        <button class="quality-toggle-btn score-apply-btn" onclick="applySourceImprovements(event)">選択した観察メモを今後の運用に取り入れる</button>
      </div>
    </details>`;
}

function sourceAppliedKey(tweetId) {
  return `x-preview-source-applied:${tweetId}`;
}

function loadSourceAppliedIds(tweetId) {
  try {
    return new Set(JSON.parse(localStorage.getItem(sourceAppliedKey(tweetId)) || '[]'));
  } catch (_) {
    return new Set();
  }
}

function saveSourceAppliedIds(tweetId, ids) {
  localStorage.setItem(sourceAppliedKey(tweetId), JSON.stringify(Array.from(ids)));
}

async function analyzeCurrentSourcePost() {
  if (!currentSourcePost) return;
  const slot = document.getElementById('source-analysis-slot');
  if (slot) slot.innerHTML = '<div class="loading">元投稿を分析中...</div>';
  try {
    const res = await apiFetch(`${API}/source-post/analysis?tweet_id=${encodeURIComponent(currentSourcePost.tweet_id)}`);
    const data = await readApiJson(res, '元投稿分析');
    if (!data.ok) throw new Error(data.error || '分析に失敗しました');
    currentSourcePost.analysis = data.analysis;
    const listItem = sourcePosts.find(item => item.tweet_id === currentSourcePost.tweet_id);
    if (listItem) listItem.analysis = data.analysis;
    renderSourceList();
    renderSourcePost(currentSourcePost);
  } catch (error) {
    if (slot) slot.innerHTML = `<div class="error-msg">分析失敗: ${esc(error.message)}</div>`;
  }
}

async function applySourceImprovements(event) {
  if (event) {
    event.preventDefault();
    event.stopPropagation();
  }
  const analysis = currentSourcePost?.analysis;
  if (!analysis) {
    showToast('先に元投稿を分析してください', true);
    return;
  }
  const selectedIds = new Set(Array.from(document.querySelectorAll('.source-transfer-check:checked')).map(input => input.value));
  const selected = (analysis.transfer_points || []).filter(item => selectedIds.has(String(item.id)));
  if (!selected.length) {
    showToast('取り入れる観察メモを選んでください', true);
    return;
  }
  try {
    const res = await apiFetch(`${API}/source-post/analysis/apply`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        tweet_id: currentSourcePost.tweet_id,
        account_key: selectedAccount,
        improvements: selected,
      }),
    });
    const data = await readApiJson(res, '元投稿改善点保存');
    if (!res.ok || data.ok === false) throw new Error(data.error || '保存に失敗しました');
    const ids = loadSourceAppliedIds(currentSourcePost.tweet_id);
    selected.forEach(item => ids.add(String(item.id)));
    saveSourceAppliedIds(currentSourcePost.tweet_id, ids);
    showToast(`✓ 元投稿の観察メモを${selected.length}件、今後の運用に取り入れました`);
    renderSourcePost(currentSourcePost);
  } catch (error) {
    showToast(`観察メモ保存失敗: ${error.message}`, true);
  }
}

// ── 分析ダッシュボード ───────────────────────────────

async function loadAnalyticsDashboard() {
  const el = document.getElementById('draft-list');
  el.innerHTML = '<div class="loading">分析データを読み込み中...</div>';
  document.getElementById('preview-content').innerHTML = '<div class="loading">ダッシュボードを作成中...</div>';
  analyticsLoading = true;
  try {
    const params = new URLSearchParams();
    if (isAccountFilterActive()) params.set('account', selectedAccount);
    const res = await apiFetch(`${API}/analytics/dashboard?${params.toString()}`);
    const data = await readApiJson(res, '分析ダッシュボード');
    if (!data.ok) throw new Error(data.error || '取得に失敗しました');
    analyticsDashboard = data;
    renderAnalyticsSidebar();
    renderAnalyticsDashboard();
  } catch (error) {
    el.innerHTML = `<div class="error-msg">分析取得エラー: ${esc(error.message)}</div>`;
    document.getElementById('preview-content').innerHTML = `<div class="error-msg">分析取得エラー: ${esc(error.message)}</div>`;
  } finally {
    analyticsLoading = false;
  }
}

function renderAnalyticsSidebar() {
  const el = document.getElementById('draft-list');
  const summary = analyticsDashboard?.summary || {};
  el.innerHTML = `
    <div class="source-list-head">
      <strong>分析レポート</strong>
      <span>${formatMetric(summary.tracked_tweets)}投稿</span>
    </div>
    <div class="analytics-side-card">
      <span>取得済み投稿</span><strong>${formatMetric(summary.tracked_tweets)}</strong>
      <span>投稿済み下書き</span><strong>${formatMetric(summary.posted_drafts)}</strong>
      <span>紐づけ済み</span><strong>${formatMetric(summary.linked_drafts)}</strong>
      <span>今回の自動紐づけ</span><strong>${formatMetric(summary.auto_linked_drafts)}</strong>
      <span>未紐づけ</span><strong>${formatMetric(summary.unlinked_drafts)}</strong>
    </div>
    <button class="load-more-btn" onclick="loadAnalyticsDashboard()" ${analyticsLoading ? 'disabled' : ''}>分析を更新</button>`;
}

function maxFromSeries(items, key) {
  return Math.max(1, ...items.map(item => Number(item[key] || 0)));
}

function renderMiniBars(items, key, label) {
  const max = maxFromSeries(items, key);
  const recent = items.slice(-24);
  return `
    <div class="mini-chart" aria-label="${escAttr(label)}">
      ${recent.map(item => `<span title="${escAttr(`${item.snapshot_date}: ${item[key] || 0}`)}" style="height:${Math.max(4, Number(item[key] || 0) / max * 100)}%"></span>`).join('')}
    </div>`;
}

function renderAnalyticsDashboard() {
  const data = analyticsDashboard || {};
  const summary = data.summary || {};
  const daily = data.daily || [];
  const topPosts = data.top_posts || [];
  const postedDrafts = data.posted_drafts || [];
  const latestDaily = daily[daily.length - 1] || {};
  const firstDaily = daily[0] || {};
  const content = document.getElementById('preview-content');
  content.innerHTML = `
    <div class="analytics-page">
      <div class="preview-header">
        <div class="preview-header-info">
          <h3>分析ダッシュボード</h3>
          <p>取得済みメトリクスと投稿済み下書きの紐づけ状況</p>
        </div>
        <div class="header-actions">
          <button class="image-source-copy-btn" onclick="loadAnalyticsDashboard()">${materialIcon('refresh')}更新</button>
        </div>
      </div>
      <section class="analytics-summary-strip">
        <span>期間 <strong>${esc(firstDaily.snapshot_date || '-')} → ${esc(latestDaily.snapshot_date || '-')}</strong></span>
        <span>最新フォロワー <strong>${formatMetric(latestDaily.followers_count)}</strong></span>
        <span>投稿済み下書き <strong>${formatMetric(summary.posted_drafts)}</strong></span>
        <span>今回自動 <strong>${formatMetric(summary.auto_linked_drafts)}</strong></span>
        <span>紐づけ率 <strong>${summary.posted_drafts ? Math.round(Number(summary.linked_drafts || 0) / Number(summary.posted_drafts || 1) * 100) : 0}%</strong></span>
      </section>
      <section class="analytics-kpis">
        ${renderKpi('取得投稿', summary.tracked_tweets)}
        ${renderKpi('imp', summary.impressions)}
        ${renderKpi('like', summary.likes)}
        ${renderKpi('ER', `${Number(summary.engagement_rate || 0).toFixed(2)}%`)}
        ${renderKpi('未紐づけ', summary.unlinked_drafts)}
      </section>
      <section class="analytics-chart-grid">
        <div class="analytics-panel analytics-chart-panel"><strong>フォロワー推移</strong>${renderMiniBars(daily, 'followers_count', 'フォロワー推移')}</div>
        <div class="analytics-panel analytics-chart-panel"><strong>累積imp</strong>${renderMiniBars(daily, 'cumulative_impressions', '累積imp')}</div>
      </section>
      <section class="analytics-panel">
        <div class="analytics-section-title"><strong>上位投稿</strong><span>反応が取れている投稿の型を見る</span></div>
        <div class="analytics-table">${topPosts.map(renderTopPostRow).join('') || '<div class="loading">投稿メトリクスがまだありません</div>'}</div>
      </section>
      <section class="analytics-panel">
        <div class="analytics-section-title"><strong>投稿済み下書きとメトリクス紐づけ</strong><span>下書きと実績値を結び、運用判断に使う</span></div>
        <div class="analytics-table">${postedDrafts.map(renderPostedDraftMetricRow).join('') || '<div class="loading">投稿済み下書きがありません</div>'}</div>
      </section>
    </div>`;
}

function renderKpi(label, value) {
  return `<div class="analytics-kpi"><span>${esc(label)}</span><strong>${typeof value === 'number' ? formatMetric(value) : esc(value)}</strong></div>`;
}

function renderTopPostRow(item) {
  const metrics = item.metrics || {};
  return `
    <div class="analytics-row">
      <div><a href="${escAttr(item.tweet_url)}" target="_blank">@${esc(item.x_username)} / ${esc(item.posted_at || '')}</a><p>${esc((item.content || '').slice(0, 100))}</p></div>
      <span>imp ${formatMetric(metrics.impressions)}</span>
      <span>like ${formatMetric(metrics.likes)}</span>
      <span>ER ${Number(metrics.engagement_rate || 0).toFixed(2)}%</span>
    </div>`;
}

function metricLinkStatus(item) {
  const status = item.match_status || (item.linked_tweet_id ? 'linked' : 'unlinked_no_candidate');
  const table = {
    linked: { label: '紐づけ済み', tone: 'ok' },
    auto_linked: { label: '自動紐づけ', tone: 'ok' },
    linked_missing_metrics: { label: '数値未取得', tone: 'warn' },
    unlinked_low_confidence: { label: '自動保留', tone: 'warn' },
    unlinked_no_candidate: { label: '未検出', tone: 'danger' },
    unlinked_conflict: { label: '重複保留', tone: 'warn' },
  };
  return table[status] || { label: '未紐づけ', tone: 'danger' };
}

function renderMetricLinkNote(item, linked, candidate) {
  if (linked) {
    const source = item.match_status === 'auto_linked' ? 'auto' : 'linked';
    return `<small>${source}: ${esc(item.linked_tweet_id)}${item.match_confidence ? ` / ${Number(item.match_confidence || 0).toFixed(2)}` : ''}</small>`;
  }
  if (candidate) {
    return `<small>候補あり: ${Number(candidate.similarity || 0).toFixed(2)} / ${esc(item.match_reason || '自動紐づけ条件未満')}</small>`;
  }
  return `<small>${esc(item.match_reason || '一致候補なし')}</small>`;
}

function renderPostedDraftMetricRow(item) {
  const linked = item.linked_tweet;
  const candidate = item.candidates?.[0];
  const metrics = linked?.metrics || {};
  const status = metricLinkStatus(item);
  return `
    <div class="analytics-row posted-link-row">
      <div>
        <button class="text-link-btn" onclick="setMainView('drafts').then(() => selectDraft('${escAttr(item.draft_id)}'))">${esc((item.memo || item.preview_content || item.draft_id).slice(0, 80))}</button>
        <p>${esc((item.preview_content || '').slice(0, 120))}</p>
        ${renderMetricLinkNote(item, linked, candidate)}
      </div>
      <span>imp ${linked ? formatMetric(metrics.impressions) : '-'}</span>
      <span>like ${linked ? formatMetric(metrics.likes) : '-'}</span>
      <span><b class="metric-link-status metric-link-${escAttr(status.tone)}">${esc(status.label)}</b></span>
    </div>`;
}

async function linkDraftMetric(draftId, tweetId) {
  try {
    const res = await apiFetch(`${API}/draft/metrics-link`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ draft_id: draftId, tweet_id: tweetId, source: 'candidate' }),
    });
    const data = await readApiJson(res, 'メトリクス紐づけ');
    if (!res.ok || data.ok === false) throw new Error(data.error || '紐づけに失敗しました');
    showToast('✓ 投稿済み下書きとメトリクスを紐づけました');
    await loadAnalyticsDashboard();
  } catch (error) {
    showToast(`紐づけ失敗: ${error.message}`, true);
  }
}

function normalizeAccountKey(accountKey) {
  if (!accountKey) return ACCOUNT_ALL_KEY;
  if (accountKey === ACCOUNT_ANONYMOUS_KEY) return ACCOUNT_ANONYMOUS_KEY;
  return accountKey;
}

function isAnonymousMode() {
  return selectedAccount === ACCOUNT_ANONYMOUS_KEY;
}

function isAccountFilterKey(accountKey) {
  return accountKey && accountKey !== ACCOUNT_ALL_KEY && accountKey !== ACCOUNT_ANONYMOUS_KEY;
}

function isAccountFilterActive() {
  return isAccountFilterKey(selectedAccount);
}

function getDraftDisplayIdentity(draft) {
  if (isAnonymousMode()) {
    return {
      anonymous: true,
      displayName: ANONYMOUS_DISPLAY_NAME,
      username: ANONYMOUS_USERNAME,
      handle: `@${ANONYMOUS_USERNAME}`,
      profileImageUrl: '',
      initial: '',
    };
  }
  const displayName = draft?.display_name || draft?.x_username || 'X';
  const username = draft?.x_username || '';
  return {
    anonymous: false,
    displayName,
    username,
    handle: username ? `@${username}` : '',
    profileImageUrl: draft?.profile_image_url || '',
    initial: (displayName || username || 'X').charAt(0).toUpperCase(),
  };
}

function renderIdentityAvatar(identity, className = 'avatar-sm') {
  const classes = `${className}${identity.anonymous ? ' is-anonymous' : ''}`;
  if (identity.profileImageUrl) {
    return `<div class="${classes}"><img src="${escAttr(identity.profileImageUrl)}" alt="${escAttr(identity.displayName)}" loading="lazy"></div>`;
  }
  if (identity.anonymous) {
    return `<div class="${classes}">${materialIcon('account_circle')}</div>`;
  }
  return `<div class="${classes}">${esc(identity.initial || 'X')}</div>`;
}

function getSelectedAccountPreset() {
  if (!isAccountFilterActive()) return null;
  return accountPresets.find(a => a.x_username === selectedAccount || a.id === selectedAccount) || null;
}

function renderAccountSummary() {
  const nameEl = document.getElementById('account-summary-name');
  const userEl = document.getElementById('account-summary-user');
  const avatarEl = document.querySelector('.account-summary-avatar');
  const preset = getSelectedAccountPreset();
  const headerAvatarEl = document.getElementById('header-account-avatar');

  avatarEl?.classList.toggle('is-anonymous', isAnonymousMode());
  headerAvatarEl?.classList.toggle('is-anonymous', isAnonymousMode());

  if (isAnonymousMode()) {
    nameEl.textContent = '匿名デモ';
    userEl.textContent = '全下書きをアカウントなしで表示';
    const anonymousIcon = materialIcon('account_circle');
    avatarEl.innerHTML = anonymousIcon;
    if (headerAvatarEl) headerAvatarEl.innerHTML = anonymousIcon;
    return;
  }

  if (!preset) {
    nameEl.textContent = 'すべてのアカウント';
    userEl.textContent = '下書き全体を表示';
    const silhouette = materialIcon('person');
    avatarEl.innerHTML = silhouette;
    if (headerAvatarEl) headerAvatarEl.innerHTML = silhouette;
    return;
  }

  nameEl.textContent = preset.display_name || preset.x_username || preset.id;
  userEl.textContent = preset.x_username ? `@${preset.x_username}` : preset.id;

  const avatarContent = preset.profile_image_url
    ? `<img src="${escAttr(preset.profile_image_url)}" alt="${escAttr(preset.display_name || preset.x_username)}" loading="lazy">`
    : esc((preset.display_name || preset.x_username || preset.id || 'X').charAt(0).toUpperCase());

  avatarEl.innerHTML = avatarContent;
  if (headerAvatarEl) headerAvatarEl.innerHTML = avatarContent;
}

function ensureAccountModal() {
  let overlay = document.getElementById('account-modal');
  if (overlay) return overlay;
  overlay = document.createElement('div');
  overlay.id = 'account-modal';
  overlay.className = 'modal-overlay';
  overlay.style.display = 'none';
  overlay.innerHTML = `
    <div class="modal account-modal" role="dialog" aria-modal="true" aria-labelledby="account-modal-title" onclick="event.stopPropagation()">
      <div class="thread-modal-header">
        <div>
          <div class="thread-step-label">プリセット</div>
          <h3 class="image-modal-title" id="account-modal-title">アカウント変更</h3>
        </div>
        <button class="thread-close-btn" onclick="closeAccountModal()" aria-label="閉じる">${materialIcon('close')}</button>
      </div>
      <div id="account-preset-list" class="account-preset-list"></div>
    </div>`;
  overlay.onclick = closeAccountModal;
  document.body.appendChild(overlay);
  return overlay;
}

async function openAccountModal() {
  const overlay = ensureAccountModal();
  if (accountPresets.length === 0) await loadAccounts();
  renderAccountPresetList();
  overlay.style.display = 'flex';
}

function closeAccountModal() {
  const overlay = document.getElementById('account-modal');
  if (overlay) overlay.style.display = 'none';
}

function renderAccountPresetList() {
  const list = document.getElementById('account-preset-list');
  if (!list) return;
  const allSelected = selectedAccount === 'all';
  const allCard = `
    <button class="account-preset-card ${allSelected ? 'active' : ''}" onclick="selectAccountPreset('all')">
      <span class="avatar-sm account-preset-avatar">${materialIcon('group', { filled: allSelected })}</span>
      <span class="account-preset-main">
        <span class="account-preset-name">すべてのアカウント</span>
        <span class="account-preset-user">全下書きを表示</span>
      </span>
      <span class="account-preset-check">${allSelected ? materialIcon('check_circle', { filled: true }) : ''}</span>
    </button>`;
  const anonymousSelected = isAnonymousMode();
  const anonymousCard = `
    <button class="account-preset-card anonymous-preset ${anonymousSelected ? 'active' : ''}" onclick="selectAccountPreset('${ACCOUNT_ANONYMOUS_KEY}')">
      <span class="avatar-sm account-preset-avatar is-anonymous">${materialIcon('account_circle', { filled: anonymousSelected })}</span>
      <span class="account-preset-main">
        <span class="account-preset-name">匿名デモ</span>
        <span class="account-preset-user">全下書きをアカウントなしで表示</span>
      </span>
      <span class="account-preset-check">${anonymousSelected ? materialIcon('check_circle', { filled: true }) : ''}</span>
    </button>`;
  const cards = accountPresets.map(account => {
    const key = account.x_username || account.id;
    const selected = selectedAccount === key;
    const initial = (account.display_name || account.x_username || account.id || 'X').charAt(0).toUpperCase();
    const avatar = account.profile_image_url
      ? `<img src="${escAttr(account.profile_image_url)}" alt="${escAttr(account.display_name || account.x_username)}" loading="lazy">`
      : esc(initial);
    return `
      <button class="account-preset-card ${selected ? 'active' : ''}" onclick="selectAccountPreset('${escAttr(key)}')">
        <span class="avatar-sm account-preset-avatar">${avatar}</span>
        <span class="account-preset-main">
          <span class="account-preset-name">${esc(account.display_name || account.x_username || account.id)}</span>
          <span class="account-preset-user">@${esc(account.x_username || account.id)}${Number(account.draft_count || 0) ? ` ・ ${Number(account.draft_count)}件` : ''}</span>
        </span>
        <span class="account-preset-check">${selected ? materialIcon('check_circle', { filled: true }) : ''}</span>
      </button>`;
  }).join('');
  list.innerHTML = allCard + anonymousCard + cards;
}

async function selectAccountPreset(accountKey) {
  const previousAccount = selectedAccount;
  selectedAccount = normalizeAccountKey(accountKey || ACCOUNT_ALL_KEY);
  localStorage.setItem('x-preview-selected-account', selectedAccount);
  renderAccountSummary();
  closeAccountModal();
  if (activeView === 'analytics') {
    await loadAnalyticsDashboard();
    return;
  }
  if (activeView === 'sources') {
    const source = currentSourcePost || sourcePosts[0];
    if (source) {
      renderSourcePost(source);
    } else {
      renderViewEmpty('元投稿を選択してください', 'ブックマーク済み投稿を分析して、自分の投稿へ転用できます');
    }
    syncMobileTabForView();
    return;
  }
  if (!isAccountFilterKey(previousAccount) && !isAccountFilterActive()) {
    renderDraftList();
    if (currentDraft) renderPreview(currentDraft);
    return;
  }
  currentDraft = null;
  await loadDraftList();
}

// ── 下書き選択 ───────────────────────────────────────

async function selectDraft(draftId) {
  document.querySelectorAll('.draft-item').forEach(el =>
    el.classList.toggle('active', el.dataset.id === draftId)
  );

  const content = document.getElementById('preview-content');
  content.innerHTML = '<div class="loading">読み込み中...</div>';

  try {
    const res   = await apiFetch(`${API}/draft?id=${draftId}`);
    const draft = await res.json();
    currentDraft = draft;
    renderPreview(draft);

    // モバイル：プレビュータブに自動切り替え
    if (window.innerWidth <= 640) switchTab('preview');

  } catch (e) {
    content.innerHTML = `<div class="error-msg">エラー: ${e.message}</div>`;
  }
}

// ── プレビュー描画 ───────────────────────────────────

function renderPreview(draft) {
  const content    = document.getElementById('preview-content');
  const isThread   = draft.parts.length > 1;
  const isPosted   = draft.status === 'published';

  // 投稿済みのときはプレビューエリア全体の背景を変える
  const previewArea = document.getElementById('preview-area');
  previewArea.classList.toggle('is-posted-bg', isPosted);
  const identity   = getDraftDisplayIdentity(draft);
  const previewUrl = `${publicUrl}?draft=${draft.draft_id}`;
  const original   = draft.original_tweet;
  const hasOrig    = !!(original && original.tweet_url);

  // ── ヘッダー ──
  const postedBar = isPosted
    ? `<div class="posted-bar">${materialIcon('check_circle', { filled: true })}投稿済み ${draft.published_at ? '— ' + draft.published_at : ''}</div>`
    : '';
  const statusBtn = isPosted
    ? `<button class="revert-draft-btn" onclick="setStatus('${draft.draft_id}','draft')">${materialIcon('undo')}未投稿に戻す</button>`
    : `<button class="post-btn" onclick="onPostClick(false)"><span class="x-mark" aria-hidden="true">𝕏</span>投稿する</button>`;
  const quotePostBtn = isPosted || !hasOrig
    ? ''
    : `<button class="post-btn quote-post-btn" onclick="onPostClick(true)"><span class="x-mark" aria-hidden="true">𝕏</span>引用リツイートする</button>`;
  const autoPostBtn = '';
  const autoQuotePostBtn = '';
  const markPostedBtn = isPosted
    ? ''
    : `<button class="mark-posted-btn" id="mark-posted-btn" onclick="setStatus('${draft.draft_id}','published')">${materialIcon('task_alt', { filled: true })}投稿済みにする</button>`;
  const fullTextRewriteBtn = `<button class="image-source-copy-btn text-rewrite-header-btn" onclick="openDraftTextRewriteModal()">${materialIcon('auto_fix_high', { filled: true })}全文AI修正</button>`;
  const draftIdStr = String(draft.draft_id);
  const serverObsidianSave = draft.obsidian_save?.saved ? draft.obsidian_save : null;
  if (serverObsidianSave) {
    savedObsidianDraftIds.add(draftIdStr);
    obsidianSaveByDraftId.set(draftIdStr, serverObsidianSave);
  }
  const obsidianSave = obsidianSaveByDraftId.get(draftIdStr) || serverObsidianSave;
  const alreadySavedToObsidian = savedObsidianDraftIds.has(draftIdStr);
  const obsidianSavedPath = obsidianSave?.relative_path || obsidianSave?.path || '';
  const canOpenObsidian = alreadySavedToObsidian && !!obsidianSave?.obsidian_url;
  const obsidianMainLabel = canOpenObsidian
    ? `${materialIcon('open_in_new')}Obsidianで開く`
    : alreadySavedToObsidian
      ? `${materialIcon('check_circle', { filled: true })}保存済み`
      : `${materialIcon('save')}Obsidianへ保存`;
  const obsidianBtn = `
    <div class="obsidian-save-controls">
      <button class="obsidian-save-btn ${alreadySavedToObsidian ? 'is-saved' : ''}" id="obsidian-save-btn" onclick="${canOpenObsidian ? 'openSavedObsidian()' : 'saveToObsidian()'}" ${alreadySavedToObsidian && !canOpenObsidian ? 'disabled' : ''}>${obsidianMainLabel}</button>
      <span class="obsidian-save-status" id="obsidian-save-status" title="${esc(obsidianSave?.path || obsidianSavedPath)}"></span>
    </div>
  `;

  const header = `
      <div class="preview-header">
      <div class="preview-header-info">
        <h3>${esc(identity.displayName)}
          <span style="color:var(--text-muted);font-weight:400;font-size:14px">
            ${esc(identity.handle)}
          </span>
        </h3>
        <p>
          ${isThread ? `ツリー (${draft.parts.length} パーツ)` : '単発'} ・ ${draft.created_at}
          ${draft.has_image ? '<span class="badge badge-image preview-image-badge">画像付き</span>' : ''}
        </p>
      </div>
      <div class="header-actions">
        ${draft.memo ? `<span class="preview-memo">${materialIcon('sticky_note_2', { filled: true })}${esc(draft.memo)}</span>` : ''}
        <div class="header-action-row header-action-primary">
          ${statusBtn}
          ${quotePostBtn}
          ${autoPostBtn}
          ${autoQuotePostBtn}
        </div>
        <div class="header-action-row header-action-secondary">
          ${obsidianBtn}
          ${fullTextRewriteBtn}
          ${markPostedBtn}
        </div>
      </div>
    </div>
    ${postedBar}
    ${renderQualityPanel(draft)}
    <div class="preview-url-bar">
      ${materialIcon('link')}<a href="${previewUrl}" target="_blank">${previewUrl}</a>
    </div>
    <div id="auto-post-progress-slot"></div>
  `;

  // ── 下書きカード列 ──
  const draftCards = buildDraftCards(draft, isThread);

  const imagePromptBlock = buildImagePromptBlock(draft);

  if (!hasOrig) {
    // 比較なし — 従来レイアウト
    content.className = 'preview-content';
    content.innerHTML = header + draftCards + imagePromptBlock;
    if (autoPostDraftId === draft.draft_id) renderAutoPostProgress(autoPostJob);
    return;
  }

  // ── 比較モード ──
  content.className = 'preview-content has-comparison';

  const origCards  = buildOriginalCards(original);
  const labelBar   = `
    <div class="comparison-labels">
      <div class="comparison-label">
        <span class="comparison-label-tag original">元投稿</span>
        @${esc(original.author_username)}${original.author_name ? ' · ' + esc(original.author_name) : ''}
      </div>
      <div class="comparison-label">
        <span class="comparison-label-tag draft">下書き</span>
        ${isThread ? `ツリー ${draft.parts.length} パーツ` : '単発'}
      </div>
    </div>`;

  const toggleBar = `
    <div class="compare-toggle-bar" id="compare-toggle-bar">
      <button class="compare-toggle-btn" id="ctoggle-original"
              onclick="switchCompareCol('original')">元投稿</button>
      <button class="compare-toggle-btn active" id="ctoggle-draft"
              onclick="switchCompareCol('draft')">下書き</button>
    </div>`;

  const body = `
    <div class="comparison-body">
      <div class="comparison-col visible" id="col-original">${origCards}</div>
      <div class="comparison-col visible" id="col-draft">${draftCards}${imagePromptBlock}</div>
    </div>`;

  content.innerHTML = header + labelBar + toggleBar + body;
  if (autoPostDraftId === draft.draft_id) renderAutoPostProgress(autoPostJob);

  // モバイル初期状態: 下書きのみ表示
  if (window.innerWidth <= 640) switchCompareCol('draft');
}

// ── 比較: 元投稿カード（oEmbed対応）────────────────

function buildOriginalCards(original) {
  if (!original || !original.tweet_url) {
    return '<div class="original-unavailable">元投稿URLが不明です</div>';
  }

  // スレッドがあれば各パーツのURL一覧、なければルートURL1件
  const urls = (original.thread_parts && original.thread_parts.length > 0)
    ? original.thread_parts.map(p => p.tweet_url).filter(Boolean)
    : [original.tweet_url];

  // プレースホルダーを並べてからoEmbedを非同期で流し込む
  const slots = urls.map((url, i) => {
    const id = `oembed-slot-${Date.now()}-${i}`;
    requestAnimationFrame(() => loadOembed(url, id));
    return `<div class="oembed-slot" id="${id}">
      <div class="oembed-loading">読み込み中...</div>
    </div>`;
  }).join('');

  return slots;
}

async function loadOembed(tweetUrl, slotId) {
  const slot = document.getElementById(slotId);
  if (!slot) return;
  try {
    const res  = await apiFetch(`${API}/oembed?url=${encodeURIComponent(tweetUrl)}`);
    const data = await res.json();
    if (!slot.isConnected) return; // 別の下書きに切り替わっていたら無視
    if (data.ok && data.html) {
      slot.innerHTML = `<div class="oembed-wrapper">${data.html}</div>`;
      // widgets.js がすでにロード済みなら再描画を要求する
      if (window.twttr && window.twttr.widgets) {
        window.twttr.widgets.load(slot);
      }
    } else {
      slot.innerHTML = `<div class="oembed-loading oembed-error">
        元投稿を読み込めませんでした
        <a href="${escAttr(tweetUrl)}" target="_blank">Xで開く</a>
      </div>`;
    }
  } catch (_) {
    if (slot.isConnected) {
      slot.innerHTML = `<div class="oembed-loading oembed-error">
        元投稿を読み込めませんでした
        <a href="${escAttr(tweetUrl)}" target="_blank">Xで開く</a>
      </div>`;
    }
  }
}

// ── 比較: 下書きカード列 ─────────────────────────────

function buildDraftCards(draft, isThread) {
  const identity = getDraftDisplayIdentity(draft);
  const cards = draft.parts.map((part, i) => {
    const isLast  = i === draft.parts.length - 1;
    const xLength = xFreeAccountLength(part.content);
    const rawCharLen = xLength.rawChars;
    const charClass = charCountCheckEnabled && xLength.over
      ? 'char-over'
      : charCountCheckEnabled && xLength.units > X_FREE_ACCOUNT_UNIT_LIMIT * 0.92
        ? 'char-warn'
        : 'char-ok';
    const charLabel = charCountCheckEnabled
      ? `${xLength.units.toLocaleString()}/${X_FREE_ACCOUNT_UNIT_LIMIT} X無料換算${xLength.over ? ` ${materialIcon('warning', { filled: true })}超過` : ''}`
      : `${rawCharLen.toLocaleString()} 文字`;
    const charTitle = charCountCheckEnabled
      ? '無料X通常ポスト基準: 全角140/半角280相当、URL=23、改行・絵文字=1'
      : '';

    const avatarEl = renderIdentityAvatar(identity, 'avatar');
    const avatarBlock = isThread && !isLast
      ? `<div class="thread-line-wrapper">${avatarEl}<div class="thread-line"></div></div>`
      : avatarEl;
    const hasImagePrompt = !!(draft.image_prompts?.[0]?.prompt || '').trim();
    const regenerateImageBtn = part.image_url && hasImagePrompt
      ? `<button class="image-source-copy-btn" onclick="openImagePromptModal('refine')">再生成</button>`
      : '';
    const imageHtml = part.image_url
      ? `<div class="tweet-image">
          <div class="tweet-image-toolbar">
            ${regenerateImageBtn}
            <button class="image-source-copy-btn image-generation-cancel-btn" onclick="detachDraftImage(${Number(part.position) || i + 1})">画像を外す</button>
            <button class="image-source-copy-btn" onclick="saveGeneratedImageFromUrl('${escAttr(part.image_url)}')">画像保存</button>
            <button class="image-source-copy-btn" onclick="copyGeneratedImageFromUrl('${escAttr(part.image_url)}')">画像をコピー</button>
          </div>
          <img src="${escAttr(part.image_url)}" alt="画像" loading="lazy">
        </div>`
      : '';

    return `
      <div class="tweet-card">
        <div class="tweet-header">
          ${avatarBlock}
          <div class="tweet-body">
            <div class="tweet-user-info">
              <span class="tweet-display-name">${esc(identity.displayName)}</span>
              ${identity.anonymous ? '' : `<span class="tweet-verified">${materialIcon('verified', { filled: true })}</span>`}
              <span class="tweet-handle">${esc(identity.handle)}</span>
            </div>
            <div class="tweet-text" id="text-${part.part_id}">${formatText(part.content)}</div>
            ${imageHtml}
            <div class="char-count ${charClass}" title="${escAttr(charTitle)}">${charLabel}</div>
            <div class="tweet-actions">
              <div class="action-btn"><span class="action-icon">${materialIcon('mode_comment')}</span><span>0</span></div>
              <div class="action-btn repost"><span class="action-icon">${materialIcon('repeat')}</span><span>0</span></div>
              <div class="action-btn like"><span class="action-icon">${materialIcon('favorite')}</span><span>0</span></div>
              <div class="action-btn"><span class="action-icon">${materialIcon('bookmark')}</span><span>0</span></div>
            </div>
          </div>
        </div>
        <div class="tweet-footer">
          <button class="edit-btn text-rewrite-btn" onclick="openTextRewriteModal('${part.part_id}')">${materialIcon('auto_fix_high', { filled: true })}本文AI修正</button>
          <button class="edit-btn" onclick="startEdit('${part.part_id}')">${materialIcon('edit')}編集</button>
        </div>
      </div>`;
  }).join('');

  const addReplyZone = `
    <div class="add-reply-zone" id="add-reply-zone">
      <button class="add-reply-btn" onclick="startAddReply()">${materialIcon('add')}リプを追加</button>
    </div>`;

  return cards + addReplyZone;
}

// ── 画像プロンプトブロック ────────────────────────────

function buildImagePromptBlock(draft) {
  const prompts = draft.image_prompts;
  const sourceImgUrl = draft.parts?.[0]?.image_url || null;
  const first   = (prompts && prompts[0]) || {};
  const copy    = (first.copy   || '').trim();
  const prompt  = (first.prompt || '').trim();

  const hasPrompt    = !!prompt;
  const hasSourceImg = !!sourceImgUrl;
  if (!hasPrompt && !hasSourceImg) return '';

  const copyLabel = copy
    ? `<span class="image-prompt-copy-label">${esc(copy)}</span>`
    : '';

  const promptCopyBtn = hasPrompt
    ? `<button class="image-prompt-copy-btn" onclick="copyImagePrompt()">プロンプトをコピー</button>`
    : '';
  const regeneratePromptBtn = hasPrompt
    ? `<button class="image-source-copy-btn" data-action="regenerate" onclick="regenerateImagePrompt()">プロンプト再生成</button>`
    : '';
  const generateBtn = hasPrompt && !hasSourceImg
    ? `<button class="image-source-copy-btn" onclick="startImageGeneration()">画像生成</button>`
    : '';
  const activeImageGeneration = imageGenerationJobId
    && imageGenerationDraftId === draft.draft_id
    && !['completed', 'failed', 'cancelled'].includes(imageGenerationJob?.status || '');
  const stopGenerationBtn = activeImageGeneration
    ? `<button class="image-source-copy-btn image-generation-cancel-btn" onclick="cancelImageGeneration()">停止して再実行</button>`
    : '';
  const refineBtn = hasPrompt && hasSourceImg
    ? `<button class="image-source-copy-btn" onclick="openImagePromptModal('refine')">再生成</button>`
    : '';
  const attachBtn = !hasSourceImg
    ? `<button class="image-source-copy-btn" onclick="openImagePromptModal('attach')">投稿画像を設定</button>`
    : '';

  const sourceImgPreview = hasSourceImg
    ? `<div class="source-img-preview"><img src="${escAttr(sourceImgUrl)}" alt="生成済み画像" loading="lazy"><a href="${escAttr(sourceImgUrl)}" target="_blank">${materialIcon('link')}${esc(sourceImgUrl.length > 70 ? sourceImgUrl.slice(0, 68) + '…' : sourceImgUrl)}</a></div>`
    : '';
  const progressMarkup = getImageGenerationProgressMarkupForDraft(draft.draft_id);
  const promptBlock = hasPrompt
    ? `<div class="image-prompt-section">
        <div class="image-prompt-section-head">
          <span>画像プロンプト</span>
          <div class="image-prompt-actions">
            ${regeneratePromptBtn}
            ${promptCopyBtn}
          </div>
        </div>
        <pre class="image-prompt-pre">${esc(prompt)}</pre>
      </div>`
    : '';

  return `
    <div class="image-prompt-block">
      <div class="image-prompt-header">
        <span class="image-prompt-title">${materialIcon('image', { filled: true })}画像</span>
        ${copyLabel}
        <div class="image-copy-btns">
          ${stopGenerationBtn}
          ${generateBtn}
          ${refineBtn}
          ${attachBtn}
        </div>
      </div>
      <div id="image-generation-progress-slot">${progressMarkup}</div>
      ${sourceImgPreview}
      ${promptBlock}
      ${renderLogoDetectionPanel(draft, hasPrompt)}
    </div>`;
}

function logoSelectionKey(draftId) {
  return `x-preview-logo-selection:${draftId}`;
}

function logoDetectionKey(draftId) {
  return `x-preview-logo-detection:${draftId}`;
}

function normalizeLogoCandidates(candidates) {
  if (!Array.isArray(candidates)) return [];
  return candidates
    .filter(candidate => candidate && typeof candidate === 'object')
    .map(candidate => ({
      ...candidate,
      name: String(candidate.name || '').trim(),
      image_url: candidate.image_url || '',
      image_path: candidate.image_path || '',
      source_url: candidate.source_url || '',
      fetch_error: candidate.fetch_error || '',
      matched_aliases: Array.isArray(candidate.matched_aliases)
        ? candidate.matched_aliases.filter(Boolean)
        : [],
      registered: !!candidate.registered,
    }))
    .filter(candidate => candidate.name);
}

function saveLogoDetectionForDraft(draftId, candidates) {
  if (!draftId) return;
  try {
    localStorage.setItem(logoDetectionKey(draftId), JSON.stringify({
      version: 1,
      saved_at: new Date().toISOString(),
      candidates: normalizeLogoCandidates(candidates),
    }));
  } catch (_) {}
}

function loadLogoDetectionForDraft(draftId) {
  if (!draftId || logoDetectionByDraftId.has(draftId)) return logoDetectionByDraftId.has(draftId);
  try {
    const raw = localStorage.getItem(logoDetectionKey(draftId));
    if (!raw) return false;
    const payload = JSON.parse(raw);
    const storedCandidates = Array.isArray(payload) ? payload : payload?.candidates;
    if (!Array.isArray(storedCandidates)) return false;
    logoDetectionByDraftId.set(draftId, normalizeLogoCandidates(storedCandidates));
    return true;
  } catch {
    return false;
  }
}

function setLogoDetectionCandidates(draftId, candidates) {
  const normalized = normalizeLogoCandidates(candidates);
  logoDetectionByDraftId.set(draftId, normalized);
  saveLogoDetectionForDraft(draftId, normalized);
  return normalized;
}

function getSelectedLogoNames(draftId) {
  try {
    const parsed = JSON.parse(localStorage.getItem(logoSelectionKey(draftId)) || '[]');
    return Array.isArray(parsed) ? parsed.filter(Boolean) : [];
  } catch {
    return [];
  }
}

function setSelectedLogoNames(draftId, names) {
  localStorage.setItem(logoSelectionKey(draftId), JSON.stringify(Array.from(new Set(names.filter(Boolean)))));
}

function hasLogoDetection(draftId) {
  loadLogoDetectionForDraft(draftId);
  return logoDetectionByDraftId.has(draftId);
}

function logoPreviewUrl(candidate) {
  if (candidate.preview_url) return candidate.preview_url;
  if (candidate.image_path && /\.(png|jpe?g|webp|gif|svg|ico)$/i.test(candidate.image_path)) {
    return `${window.location.origin}/local-image?path=${encodeURIComponent(candidate.image_path)}`;
  }
  return candidate.image_url || '';
}

function renderLogoDetectionPanel(draft, hasPrompt = false) {
  loadLogoDetectionForDraft(draft.draft_id);
  const candidates = logoDetectionByDraftId.get(draft.draft_id);
  const isDetecting = logoDetectionLoadingDraftId === draft.draft_id;
  if (!candidates) {
    if (!hasPrompt) return '';
    return `
      <div class="logo-detection-panel logo-detection-panel-empty">
        <div class="logo-detection-head">
          <div>
            <strong>ツール・ロゴ</strong>
            <span>画像にロゴを入れる必要がある時だけ設定します</span>
          </div>
          <div class="logo-detection-head-actions">
            <button class="image-source-copy-btn" onclick="detectLogosForCurrentDraft()" ${isDetecting ? 'disabled' : ''}>${isDetecting ? '検出中...' : 'ツール検出'}</button>
            <button class="image-source-copy-btn" onclick='addManualLogoCandidate(${JSON.stringify(draft.draft_id)})'>ツール名を追加</button>
          </div>
        </div>
      </div>`;
  }
  const selected = new Set(getSelectedLogoNames(draft.draft_id));
  const showHidden = logoShowHiddenDraftIds.has(draft.draft_id);
  const visibleCandidates = showHidden ? candidates : candidates.filter(candidate => selected.has(candidate.name || ''));
  const hiddenCount = Math.max(0, candidates.length - visibleCandidates.length);
  const rows = visibleCandidates.length
    ? visibleCandidates.map((candidate, index) => {
        const name = candidate.name || '';
        const previewUrl = logoPreviewUrl(candidate);
        const checked = selected.has(name);
        const statusLabel = candidate.registered
          ? '登録済み'
          : candidate.image_path || candidate.image_url
            ? '取得済み・確認待ち'
            : candidate.fetch_error
              ? '取得失敗'
              : '未登録';
        const source = candidate.source_url
          ? `<a href="${escAttr(candidate.source_url)}" target="_blank">公式候補</a>`
          : '';
        const confirmButton = !candidate.registered && (candidate.image_path || candidate.image_url)
          ? `<button class="logo-action-btn ok" onclick='event.preventDefault(); event.stopPropagation(); confirmLogoCandidate(${JSON.stringify(draft.draft_id)}, ${JSON.stringify(name)})'>このロゴでOK</button>`
          : '';
        const manualButton = !candidate.registered
          ? `<button class="logo-action-btn" onclick='event.preventDefault(); event.stopPropagation(); openManualLogoModal(${JSON.stringify(draft.draft_id)}, ${JSON.stringify(name)})'>手動登録</button>`
          : '';
        const preview = previewUrl
          ? `<img class="logo-detection-thumb" src="${escAttr(previewUrl)}" alt="${escAttr(name)} logo" loading="lazy">`
          : '<span class="logo-detection-thumb empty">?</span>';
        return `
          <label class="logo-detection-row ${checked ? '' : 'is-hidden-candidate'}">
            <input type="checkbox" ${checked ? 'checked' : ''} onchange='toggleLogoSelection(${JSON.stringify(draft.draft_id)}, ${JSON.stringify(name)}, this.checked)'>
            ${preview}
            <span class="logo-detection-main">
              <span class="logo-detection-name">${esc(name)}</span>
              <span class="logo-detection-meta">${esc(statusLabel)}${source ? ` · ${source}` : ''}${candidate.fetch_error ? ` · ${esc(candidate.fetch_error)}` : ''}</span>
              <span class="logo-detection-actions">${confirmButton}${manualButton}</span>
            </span>
          </label>`;
      }).join('')
    : '<div class="logo-detection-empty">表示中のロゴ候補はありません。必要なツール名があれば追加してください。</div>';
  const selectedCount = selected.size;
  const hiddenLabel = hiddenCount ? ` / ${hiddenCount}件を非表示` : '';
  const toggleHiddenButton = hiddenCount || showHidden
    ? `<button class="image-source-copy-btn" onclick='toggleHiddenLogoCandidates(${JSON.stringify(draft.draft_id)})'>${showHidden ? '非表示を隠す' : '非表示も表示'}</button>`
    : '';
  const registerSelectedButton = selectedCount
    ? `<button class="image-source-copy-btn" onclick="registerSelectedLogos()" ${logoRegisterLoadingDraftId === draft.draft_id ? 'disabled' : ''}>${logoRegisterLoadingDraftId === draft.draft_id ? '取得中...' : '選択ロゴを取得'}</button>`
    : '';
  const note = candidates.length
    ? '<div class="logo-detection-note">取得したロゴは「このロゴでOK」を押すまで辞書登録しません。登録済みロゴだけが画像生成時に参照画像として添付されます。</div>'
    : '';
  return `
    <div class="logo-detection-panel">
      <div class="logo-detection-head">
        <div>
          <strong>ツール・ロゴ</strong>
          <span>${selectedCount}件を画像生成に使います${hiddenLabel}</span>
        </div>
        <div class="logo-detection-head-actions">
          ${toggleHiddenButton}
          <button class="image-source-copy-btn" onclick='addManualLogoCandidate(${JSON.stringify(draft.draft_id)})'>ツール名を追加</button>
          ${registerSelectedButton}
        </div>
      </div>
      <div class="logo-detection-list">${rows}</div>
      ${note}
    </div>`;
}

function toggleLogoSelection(draftId, name, checked) {
  const selected = new Set(getSelectedLogoNames(draftId));
  if (checked) selected.add(name);
  else selected.delete(name);
  setSelectedLogoNames(draftId, Array.from(selected));
  if (currentDraft?.draft_id === draftId) renderPreview(currentDraft);
}

function toggleHiddenLogoCandidates(draftId) {
  if (logoShowHiddenDraftIds.has(draftId)) logoShowHiddenDraftIds.delete(draftId);
  else logoShowHiddenDraftIds.add(draftId);
  if (currentDraft?.draft_id === draftId) renderPreview(currentDraft);
}

function addManualLogoCandidate(draftId) {
  const name = window.prompt('追加するツール名・サービス名');
  const cleanName = (name || '').trim();
  if (!cleanName) return;
  const candidates = logoDetectionByDraftId.get(draftId) || [];
  const exists = candidates.some(item => String(item.name || '').toLowerCase() === cleanName.toLowerCase());
  if (!exists) {
    setLogoDetectionCandidates(draftId, [
      ...candidates,
      {
        name: cleanName,
        registered: false,
        image_url: '',
        image_path: '',
        source_url: '',
        matched_aliases: [cleanName],
        reason: '手動追加',
      },
    ]);
  }
  const selected = new Set(getSelectedLogoNames(draftId));
  selected.add(cleanName);
  setSelectedLogoNames(draftId, Array.from(selected));
  logoShowHiddenDraftIds.add(draftId);
  if (currentDraft?.draft_id === draftId) renderPreview(currentDraft);
}

function ensureLogoManualModal() {
  let overlay = document.getElementById('logo-manual-modal');
  if (overlay) return overlay;
  overlay = document.createElement('div');
  overlay.id = 'logo-manual-modal';
  overlay.className = 'modal-overlay';
  overlay.style.display = 'none';
  overlay.innerHTML = `
    <div class="modal image-modal logo-manual-modal" role="dialog" aria-modal="true" aria-labelledby="logo-manual-title" onclick="event.stopPropagation()">
      <div class="thread-modal-header">
        <div>
          <div class="thread-step-label">ロゴ辞書</div>
          <h3 class="image-modal-title" id="logo-manual-title">ロゴを手動登録</h3>
        </div>
        <button class="thread-close-btn" onclick="closeLogoManualModal()" aria-label="閉じる">${materialIcon('close')}</button>
      </div>
      <label class="image-modal-label">ツール/サービス名</label>
      <input class="image-modal-input" id="manual-logo-name" type="text" placeholder="Remotion">
      <label class="image-modal-label">ロゴ画像URL</label>
      <input class="image-modal-input" id="manual-logo-image-url" type="url" placeholder="https://.../logo.png">
      <label class="image-modal-label">ロゴ画像ローカルパス</label>
      <input class="image-modal-input" id="manual-logo-image-path" type="text" placeholder="/Users/.../logo.png">
      <label class="image-modal-label">公式サイトURL</label>
      <input class="image-modal-input" id="manual-logo-source-url" type="url" placeholder="https://www.remotion.dev/">
      <label class="image-modal-label">別名（任意・カンマ区切り）</label>
      <input class="image-modal-input" id="manual-logo-aliases" type="text" placeholder="remotion, リモーション">
      <p class="image-modal-help">画像URLかローカルパスのどちらかを入れてください。保存後は辞書に登録され、画像生成時に参照画像として使えます。</p>
      <div class="image-modal-actions">
        <button class="cancel-btn" onclick="closeLogoManualModal()">閉じる</button>
        <button class="save-btn" onclick="saveManualLogo()">保存して登録</button>
      </div>
    </div>`;
  overlay.addEventListener('click', closeLogoManualModal);
  document.body.appendChild(overlay);
  return overlay;
}

function openManualLogoModal(draftId, name) {
  const candidates = logoDetectionByDraftId.get(draftId) || [];
  const candidate = candidates.find(item => item.name === name) || { name };
  logoManualTarget = { draftId, name };
  const modal = ensureLogoManualModal();
  modal.style.display = 'flex';
  document.getElementById('manual-logo-name').value = candidate.name || name || '';
  document.getElementById('manual-logo-image-url').value = candidate.image_url || '';
  document.getElementById('manual-logo-image-path').value = candidate.image_path || '';
  document.getElementById('manual-logo-source-url').value = candidate.source_url || '';
  document.getElementById('manual-logo-aliases').value = (candidate.matched_aliases || []).join(', ');
}

function closeLogoManualModal() {
  const modal = document.getElementById('logo-manual-modal');
  if (modal) modal.style.display = 'none';
}

function localImagePathFromUrl(imageUrl) {
  const value = (imageUrl || '').trim();
  if (!value) return '';
  try {
    const parsed = new URL(value, window.location.origin);
    if (parsed.pathname === '/local-image') {
      return parsed.searchParams.get('path') || value;
    }
  } catch (_) {}
  return value;
}

function generatedImageAttachValueForCurrentDraft() {
  const draftId = currentDraft?.draft_id;
  if (imageGenerationDraftId === draftId && imageGenerationJob?.image_path) {
    return imageGenerationJob.image_path;
  }
  const attachedImageUrl = currentDraft?.parts?.find(part => (part.image_url || '').trim())?.image_url || '';
  return localImagePathFromUrl(attachedImageUrl);
}

function referenceImagePreviewUrl() {
  const url = document.getElementById('reference-image-url')?.value?.trim() || '';
  if (url) return url;
  const path = document.getElementById('reference-image-path')?.value?.trim() || '';
  if (!path) return '';
  return `${window.location.origin}/local-image?path=${encodeURIComponent(localImagePathFromUrl(path))}`;
}

function updateReferenceImagePreview() {
  const preview = document.getElementById('image-refine-reference-preview');
  const previewButton = document.getElementById('reference-image-upload-trigger');
  if (!preview) return;
  const nextUrl = referenceImagePreviewUrl();
  if (nextUrl) {
    preview.src = nextUrl;
    preview.hidden = false;
    if (previewButton) previewButton.classList.add('has-image');
  } else {
    preview.removeAttribute('src');
    preview.hidden = true;
    if (previewButton) previewButton.classList.remove('has-image');
  }
}

function selectReferenceImageFile() {
  document.getElementById('reference-image-file-input')?.click();
}

async function uploadReferenceImageFile(file) {
  if (!file) return;
  const form = new FormData();
  form.append('image', file);
  try {
    showToast('参照画像をアップロードしています');
    const res = await apiFetch(`${API}/reference-image/upload`, {
      method: 'POST',
      body: form,
    });
    const data = await readApiJson(res, '参照画像アップロードAPI');
    if (!data.ok) throw new Error(data.error || '不明');
    const urlInput = document.getElementById('reference-image-url');
    const pathInput = document.getElementById('reference-image-path');
    if (urlInput) urlInput.value = '';
    if (pathInput) pathInput.value = data.local_path || '';
    updateReferenceImagePreview();
    showToast('✓ 参照画像をアップロードしました');
  } catch (e) {
    showToast(`参照画像アップロード失敗: ${e.message}`, true);
  } finally {
    const input = document.getElementById('reference-image-file-input');
    if (input) input.value = '';
  }
}

function attachReferenceImageForRegeneration() {
  const url = document.getElementById('reference-image-url')?.value?.trim() || '';
  const path = document.getElementById('reference-image-path')?.value?.trim() || '';
  if (!url && !path) {
    showToast('参照画像のURLまたはローカルパスを入力してください', true);
    return;
  }
  updateReferenceImagePreview();
  showToast('✓ 参照画像を再生成に使います');
}

function ensureImagePromptModal() {
  let overlay = document.getElementById('image-prompt-modal');
  if (overlay) return overlay;

  overlay = document.createElement('div');
  overlay.id = 'image-prompt-modal';
  overlay.className = 'modal-overlay';
  overlay.style.display = 'none';
  overlay.innerHTML = `
    <div class="modal image-modal" role="dialog" aria-modal="true" aria-labelledby="image-modal-title" onclick="event.stopPropagation()">
      <div class="thread-modal-header">
        <div>
          <div class="thread-step-label" id="image-modal-label">画像プロンプト</div>
          <h3 class="image-modal-title" id="image-modal-title">画像生成</h3>
        </div>
        <button class="thread-close-btn" onclick="closeImagePromptModal()" aria-label="閉じる">${materialIcon('close')}</button>
      </div>
      <div class="image-modal-field" data-field="copy">
        <label class="image-modal-label">画像コピー</label>
        <input class="image-modal-input" id="image-copy-input" type="text" placeholder="画像内コピー">
      </div>
      <div class="image-modal-field" data-field="prompt">
        <label class="image-modal-label">画像プロンプト</label>
        <textarea class="image-modal-textarea" id="image-prompt-textarea" rows="9"></textarea>
      </div>
      <div class="image-modal-field" data-field="instruction">
        <label class="image-modal-label" id="image-rewrite-label">修正/追記指示</label>
        <textarea class="image-modal-instruction" id="image-rewrite-instruction" rows="3"
          placeholder="例: もっとスマホで読みやすく。文字を減らして、3ステップを強調して"></textarea>
      </div>
      <label class="image-modal-checkbox" id="rewrite-preference-row" style="display:none">
        <input id="remember-rewrite-preference" type="checkbox">
        <span>今後もこのアカウントでは同じ方針で気をつける</span>
      </label>
      <div id="image-refine-fields" class="image-refine-fields" style="display:none">
        <div class="image-refine-preview-grid">
          <div>
            <span class="image-modal-label">現在の生成画像</span>
            <img id="image-refine-current-preview" class="image-refine-preview" alt="現在の生成画像">
          </div>
          <div>
            <span class="image-modal-label">参照画像プレビュー</span>
            <button class="image-refine-preview-upload" id="reference-image-upload-trigger" type="button" onclick="selectReferenceImageFile()">
              <img id="image-refine-reference-preview" class="image-refine-preview" alt="参照画像" hidden>
              <span class="reference-image-upload-empty">${materialIcon('add_photo_alternate')}参照画像をアップロード</span>
            </button>
            <input id="reference-image-file-input" type="file" accept="image/*" hidden onchange="uploadReferenceImageFile(this.files && this.files[0])">
            <p class="image-preview-note">再生成の参考にする画像です。投稿には添付されません。</p>
          </div>
        </div>
        <label class="image-modal-label">参照画像URL</label>
        <input class="image-modal-input" id="reference-image-url" type="url"
          placeholder="https://.../codex-logo.png">
        <label class="image-modal-label">参照画像ローカルパス</label>
        <input class="image-modal-input" id="reference-image-path" type="text"
          placeholder="/Users/.../reference.png">
      </div>
      <div class="image-modal-field" data-field="image-path">
        <label class="image-modal-label">投稿画像に設定する生成画像URL/パス</label>
        <div class="image-path-attach-row">
          <input class="image-modal-input" id="generated-image-path" type="text"
            placeholder="/local-image?path=/Users/.../image.png">
          <button class="image-source-copy-btn" data-action="attach" onclick="attachGeneratedImage()">投稿画像に設定</button>
        </div>
      </div>
      <p class="image-modal-help" id="image-modal-help"></p>
      <div id="image-rewrite-progress-slot"></div>
      <div class="image-modal-actions">
        <button class="cancel-btn" onclick="closeImagePromptModal()">閉じる</button>
        <button class="image-source-copy-btn" data-action="copy-request" onclick="copyImageGenerationRequest()">生成依頼をコピー</button>
        <button class="image-source-copy-btn" data-action="regenerate" onclick="regenerateImagePrompt()">本文から再生成</button>
        <button class="image-source-copy-btn" id="image-refine-start-btn" onclick="startImageRefinement()" style="display:none">再生成</button>
        <button class="save-btn" data-action="save" onclick="saveImagePrompt()">保存</button>
      </div>
    </div>`;
  overlay.onclick = closeImagePromptModal;
  document.body.appendChild(overlay);
  return overlay;
}

function openImagePromptModal(mode = 'generate') {
  if (!currentDraft) return;
  if (mode === 'rewrite') {
    showToast('画像プロンプトのAIリライトは無効です。画像の再生成または画像プロンプト再生成を使ってください', true);
    return;
  }
  imagePromptModalMode = mode;
  const overlay = ensureImagePromptModal();
  const first = currentDraft.image_prompts?.[0] || {};
  document.getElementById('image-copy-input').value = first.copy || '';
  document.getElementById('image-prompt-textarea').value = first.prompt || '';
  document.getElementById('image-rewrite-instruction').value = '';
  const rememberRewritePreference = document.getElementById('remember-rewrite-preference');
  if (rememberRewritePreference) rememberRewritePreference.checked = false;
  document.getElementById('generated-image-path').value = generatedImageAttachValueForCurrentDraft();
  const rewriteProgressSlot = document.getElementById('image-rewrite-progress-slot');
  if (rewriteProgressSlot) rewriteProgressSlot.innerHTML = '';
  const refineFields = document.getElementById('image-refine-fields');
  const referenceUrlInput = document.getElementById('reference-image-url');
  const referencePathInput = document.getElementById('reference-image-path');
  const currentPreview = document.getElementById('image-refine-current-preview');
  const referencePreview = document.getElementById('image-refine-reference-preview');
  if (referenceUrlInput) referenceUrlInput.value = '';
  if (referencePathInput) referencePathInput.value = '';
  if (currentPreview) currentPreview.src = currentDraft.parts?.[0]?.image_url || '';
  if (referencePreview) referencePreview.removeAttribute('src');
  if (refineFields) refineFields.style.display = mode === 'refine' ? 'block' : 'none';
  const refineStartBtn = document.getElementById('image-refine-start-btn');
  if (refineStartBtn) refineStartBtn.style.display = mode === 'refine' ? 'inline-flex' : 'none';
  if (referenceUrlInput) {
    referenceUrlInput.oninput = () => {
      updateReferenceImagePreview();
    };
  }
  if (referencePathInput) {
    referencePathInput.oninput = updateReferenceImagePreview;
  }

  configureImagePromptModalMode(mode);

  const title = mode === 'regenerate' ? '画像プロンプト再生成' : mode === 'attach' ? '投稿画像を設定' : mode === 'refine' ? '画像の再生成' : 'Codex画像生成';
  document.getElementById('image-modal-title').textContent = title;
  document.getElementById('image-modal-help').textContent =
    mode === 'generate'
      ? 'APIは使いません。生成依頼をコピーしてCodex/ChatGPTサブスク内の画像生成に渡してください。生成後の画像パスを貼ると1投稿目の投稿画像に設定できます。'
      : mode === 'regenerate'
          ? '下書き本文・キャラクター参照・選択済みロゴをもとに、画像プロンプトを作り直します。'
          : mode === 'refine'
            ? '現在の生成画像に対して、参照画像と指示を渡して再生成します。例: Codexロゴは参照画像の形に合わせる。'
            : '生成済み画像のローカルパスを貼ると、1投稿目の投稿画像としてプレビューに設定します。参照画像ではありません。';
  overlay.style.display = 'flex';
}

function configureImagePromptModalMode(mode) {
  const overlay = document.getElementById('image-prompt-modal');
  if (!overlay) return;
  const isRegenerate = mode === 'regenerate';
  const isRefine = mode === 'refine';
  const isAttach = mode === 'attach';
  overlay.querySelector('.image-modal')?.classList.toggle('rewrite-only', isRegenerate);
  overlay.querySelectorAll('[data-field]').forEach(el => {
    const field = el.dataset.field;
    const visible = isRegenerate
      ? field === 'instruction'
      : isRefine
        ? field !== 'image-path'
        : isAttach
          ? field === 'image-path'
          : true;
    el.style.display = visible ? '' : 'none';
  });
  overlay.querySelectorAll('[data-action]').forEach(el => {
    const action = el.dataset.action;
    if (isRegenerate) {
      el.style.display = action === 'regenerate' ? 'inline-flex' : 'none';
    } else {
      el.style.display = action === 'regenerate' ? 'none' : '';
    }
  });
  const label = document.getElementById('image-rewrite-label');
  const instruction = document.getElementById('image-rewrite-instruction');
  const preferenceRow = document.getElementById('rewrite-preference-row');
  if (preferenceRow) preferenceRow.style.display = 'none';
  if (label) {
    label.textContent = isRegenerate
      ? '再生成の追加指示（任意）'
      : mode === 'refine'
        ? '再生成の指示'
        : '修正/追記指示';
  }
  if (instruction) {
    instruction.rows = isRegenerate ? 8 : 3;
    instruction.placeholder = isRegenerate
        ? '例: 既存案に引っ張られず、投稿本文からシンプルな3ステップ図解に作り直して'
        : mode === 'refine'
          ? '例: ロゴは参照画像の形に合わせる。文字を減らして余白を広げる'
          : '例: もっとスマホで読みやすく。文字を減らして、3ステップを強調して';
  }
}

function closeImagePromptModal() {
  const overlay = document.getElementById('image-prompt-modal');
  if (overlay) overlay.style.display = 'none';
}

function getImageModalValues() {
  return {
    copy: document.getElementById('image-copy-input')?.value?.trim() || '',
    prompt: document.getElementById('image-prompt-textarea')?.value?.trim() || '',
    instruction: document.getElementById('image-rewrite-instruction')?.value?.trim() || '',
    imagePath: document.getElementById('generated-image-path')?.value?.trim() || '',
    referenceImageUrl: document.getElementById('reference-image-url')?.value?.trim() || '',
    referenceImagePath: document.getElementById('reference-image-path')?.value?.trim() || '',
    rememberRewritePreference: !!document.getElementById('remember-rewrite-preference')?.checked,
  };
}

async function copyImageGenerationRequest() {
  const { copy, prompt, instruction, referenceImageUrl, referenceImagePath } = getImageModalValues();
  if (!prompt) { showToast('画像プロンプトがありません', true); return; }
  const request = buildImageGenerationRequest(copy, prompt, {
    feedback: imagePromptModalMode === 'refine' ? instruction : '',
    currentImageUrl: imagePromptModalMode === 'refine' ? currentDraft?.parts?.[0]?.image_url || '' : '',
    referenceImageUrl,
    referenceImagePath,
  });
  try {
    await navigator.clipboard.writeText(request);
    showToast('✓ 画像生成依頼をコピーしました');
  } catch {
    showToast('コピーに失敗しました', true);
  }
}

async function copyActiveImageGenerationRequest() {
  if (!imageGenerationJob?.request) return;
  try {
    await navigator.clipboard.writeText(imageGenerationJob.request);
    showToast('✓ 画像生成依頼を再コピーしました');
  } catch {
    showToast('コピーに失敗しました', true);
  }
}

async function detectLogosForCurrentDraft() {
  if (!currentDraft) return;
  const first = currentDraft.image_prompts?.[0] || {};
  const copy = (first.copy || '').trim();
  const prompt = (first.prompt || '').trim();
  logoDetectionLoadingDraftId = currentDraft.draft_id;
  renderPreview(currentDraft);
  try {
    const res = await apiFetch(`${API}/logos/detect`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ draft_id: currentDraft.draft_id, copy, prompt }),
    });
    const data = await res.json();
    if (!data.ok) throw new Error(data.error || '不明');
    const candidates = setLogoDetectionCandidates(currentDraft.draft_id, data.candidates || []);
    if (!getSelectedLogoNames(currentDraft.draft_id).length) {
      setSelectedLogoNames(currentDraft.draft_id, candidates.map(candidate => candidate.name).filter(Boolean));
    }
    showToast(candidates.length ? `✓ ${candidates.length}件のツール候補を検出しました` : 'ツール候補は見つかりませんでした');
  } catch (e) {
    showToast(`ツール検出失敗: ${e.message}`, true);
  } finally {
    logoDetectionLoadingDraftId = null;
    if (currentDraft) renderPreview(currentDraft);
  }
}

async function registerSelectedLogos() {
  if (!currentDraft) return;
  const draftId = currentDraft.draft_id;
  const candidates = logoDetectionByDraftId.get(draftId) || [];
  const selected = new Set(getSelectedLogoNames(draftId));
  const targets = candidates
    .filter(candidate => selected.has(candidate.name) && !(candidate.image_path || candidate.image_url))
    .map(candidate => ({ name: candidate.name, source_url: candidate.source_url || '' }));
  if (!targets.length) {
    showToast('取得が必要な未登録ロゴはありません');
    return;
  }
  logoRegisterLoadingDraftId = draftId;
  renderPreview(currentDraft);
  try {
    const res = await apiFetch(`${API}/logos/fetch-candidates`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tools: targets }),
    });
    const data = await res.json();
    if (!data.ok) throw new Error(data.error || '不明');
    const byName = new Map((data.results || []).filter(item => item.ok).map(item => [item.name, item]));
    const failuresByName = new Map((data.results || []).filter(item => !item.ok).map(item => [item.name, item]));
    const next = candidates.map(candidate => byName.has(candidate.name)
      ? { ...candidate, ...byName.get(candidate.name), registered: !!byName.get(candidate.name).registered, fetch_error: '' }
      : failuresByName.has(candidate.name)
        ? { ...candidate, fetch_error: failuresByName.get(candidate.name).error || '取得失敗' }
        : candidate);
    setLogoDetectionCandidates(draftId, next);
    const failed = (data.results || []).filter(item => !item.ok);
    if (failed.length) {
      showToast(`一部ロゴ取得に失敗: ${failed.map(item => item.name).join(', ')}`, true);
    } else {
      showToast('✓ 選択ロゴを取得しました。OKで辞書登録してください');
    }
  } catch (e) {
    showToast(`ロゴ取得失敗: ${e.message}`, true);
  } finally {
    logoRegisterLoadingDraftId = null;
    if (currentDraft) renderPreview(currentDraft);
  }
}

async function confirmLogoCandidate(draftId, name) {
  const candidates = logoDetectionByDraftId.get(draftId) || [];
  const candidate = candidates.find(item => item.name === name);
  if (!candidate) return;
  try {
    const res = await apiFetch(`${API}/logos/register`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        name: candidate.name,
        aliases: candidate.matched_aliases || [candidate.name],
        image_url: candidate.image_url || '',
        image_path: candidate.image_path || '',
        source_url: candidate.source_url || '',
      }),
    });
    const data = await res.json();
    if (!data.ok) throw new Error(data.error || '不明');
    setLogoDetectionCandidates(draftId, candidates.map(item => item.name === name ? { ...item, ...data, registered: true, fetch_error: '' } : item));
    showToast(`✓ ${name} をロゴ辞書に登録しました`);
  } catch (e) {
    showToast(`ロゴ登録失敗: ${e.message}`, true);
  } finally {
    if (currentDraft?.draft_id === draftId) renderPreview(currentDraft);
  }
}

async function saveManualLogo() {
  if (!logoManualTarget) return;
  const draftId = logoManualTarget.draftId;
  const payload = {
    name: document.getElementById('manual-logo-name')?.value?.trim() || '',
    image_url: document.getElementById('manual-logo-image-url')?.value?.trim() || '',
    image_path: document.getElementById('manual-logo-image-path')?.value?.trim() || '',
    source_url: document.getElementById('manual-logo-source-url')?.value?.trim() || '',
    aliases: document.getElementById('manual-logo-aliases')?.value?.trim() || '',
  };
  if (!payload.name) {
    showToast('ツール/サービス名を入力してください', true);
    return;
  }
  if (!payload.image_url && !payload.image_path) {
    showToast('ロゴ画像URLかローカルパスを入力してください', true);
    return;
  }
  try {
    const res = await apiFetch(`${API}/logos/register`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (!data.ok) throw new Error(data.error || '不明');
    const candidates = logoDetectionByDraftId.get(draftId) || [];
    const exists = candidates.some(item => item.name === logoManualTarget.name || item.name === payload.name);
    const next = exists
      ? candidates.map(item => (item.name === logoManualTarget.name || item.name === payload.name) ? { ...item, ...data, registered: true, fetch_error: '' } : item)
      : [{ ...data, registered: true }, ...candidates];
    setLogoDetectionCandidates(draftId, next);
    const selected = new Set(getSelectedLogoNames(draftId));
    selected.add(data.name || payload.name);
    setSelectedLogoNames(draftId, Array.from(selected));
    closeLogoManualModal();
    showToast(`✓ ${data.name || payload.name} をロゴ辞書に登録しました`);
  } catch (e) {
    showToast(`手動ロゴ登録失敗: ${e.message}`, true);
  } finally {
    if (currentDraft?.draft_id === draftId) renderPreview(currentDraft);
  }
}

function selectedLogoNamesForGeneration(draftId) {
  return hasLogoDetection(draftId) ? getSelectedLogoNames(draftId) : undefined;
}

async function startImageGeneration(options = {}) {
  if (!currentDraft) return;
  const first = currentDraft.image_prompts?.[0] || {};
  const copy = (first.copy || '').trim();
  const prompt = (first.prompt || '').trim();
  if (!prompt) { showToast('画像プロンプトがありません', true); return; }

  clearInterval(imageGenerationTimer);
  try {
    const res = await apiFetch(`${API}/image-generation/start`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        draft_id: currentDraft.draft_id,
        copy,
        prompt,
        character_reference_url: getCharacterReferenceUrl(),
        character_traits: getCharacterTraits(),
        character_negative: getCharacterNegative(),
        character_placement: getCharacterPlacement(),
        selected_logo_names: selectedLogoNamesForGeneration(currentDraft.draft_id),
      }),
    });
    const data = await res.json();
    if (!data.ok) throw new Error(data.error || '不明');
    imageGenerationJob = data;
    imageGenerationJobId = data.job_id || null;
    imageGenerationDraftId = currentDraft.draft_id;
    pendingAutoPostAfterImage = null;
    renderImageGenerationProgress(data);
    showToast('Codex App Serverで画像生成を開始しました');
    imageGenerationTimer = setInterval(pollImageGeneration, 2500);
  } catch (e) {
    showToast(`画像生成開始失敗: ${e.message}`, true);
  }
}

async function startImageRefinement() {
  if (!currentDraft) return;
  const { copy, prompt, instruction, referenceImageUrl, referenceImagePath } = getImageModalValues();
  const currentImageUrl = currentDraft.parts?.[0]?.image_url || '';
  if (!prompt) { showToast('画像プロンプトがありません', true); return; }
  if (!currentImageUrl) { showToast('修正元の画像がありません', true); return; }
  if (!instruction) { showToast('修正フィードバックを入力してください', true); return; }

  clearInterval(imageGenerationTimer);
  try {
    const res = await apiFetch(`${API}/image-generation/start`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        draft_id: currentDraft.draft_id,
        copy,
        prompt,
        feedback: instruction,
        current_image_url: currentImageUrl,
        reference_image_url: referenceImageUrl,
        reference_image_path: referenceImagePath,
        character_reference_url: getCharacterReferenceUrl(),
        character_traits: getCharacterTraits(),
        character_negative: getCharacterNegative(),
        character_placement: getCharacterPlacement(),
        selected_logo_names: selectedLogoNamesForGeneration(currentDraft.draft_id),
      }),
    });
    const data = await res.json();
    if (!data.ok) throw new Error(data.error || '不明');
    imageGenerationJob = data;
    imageGenerationJobId = data.job_id || null;
    imageGenerationDraftId = currentDraft.draft_id;
    closeImagePromptModal();
    renderImageGenerationProgress(data);
    showToast('修正フィードバック付きで再生成を開始しました');
    imageGenerationTimer = setInterval(pollImageGeneration, 2500);
  } catch (e) {
    showToast(`再生成開始失敗: ${e.message}`, true);
  }
}

function getImageGenerationProgressMarkupForDraft(draftId) {
  if (!imageGenerationJob || imageGenerationDraftId !== draftId) return '';
  return buildImageGenerationProgressMarkup(imageGenerationJob);
}

function buildImageGenerationProgressMarkup(job) {
  if (!job) return '';
  const progress = Math.max(0, Math.min(100, Number(job.progress || 0)));
  const label = job.message || (
    job.status === 'completed'
      ? '画像生成完了。プレビューに反映しました。'
      : job.status === 'failed'
        ? '画像生成に失敗しました。'
        : 'Codex App Serverで画像生成中です。'
  );
  const retryButton = ['failed', 'cancelled'].includes(job.status) && job.request
    ? `<button class="image-generation-copy-btn" onclick="copyActiveImageGenerationRequest()">生成依頼をコピー</button>`
    : '';
  const jobId = job.job_id || imageGenerationJobId;
  const canCancel = jobId && !['completed', 'failed', 'cancelled'].includes(job.status);
  const cancelButton = canCancel
    ? '<button class="image-generation-copy-btn image-generation-cancel-btn" onclick="cancelImageGeneration()">停止して再実行</button>'
    : '';
  const logs = Array.isArray(job.logs) ? job.logs.slice(-12) : [];
  const logBlock = logs.length
    ? `<div class="image-generation-log">
        ${logs.map(log => `
          <div class="image-generation-log-row ${escAttr(log.level || 'info')}">
            <span class="image-generation-log-time">${esc(log.at || '')}</span>
            <span class="image-generation-log-message">${esc(log.message || '')}</span>
          </div>`).join('')}
      </div>`
    : '';
  return `
    <div class="image-generation-progress ${escAttr(job.status || '')} ${job.status === 'failed' ? 'failed' : ''}">
      <div class="image-generation-progress-top">
        <span class="image-generation-status-text">${esc(label)}</span>
        <span class="image-generation-status-actions">
          <span>${progress}%</span>
          ${cancelButton}
          ${retryButton}
        </span>
      </div>
      <div class="image-generation-bar"><div style="width:${progress}%"></div></div>
      ${logBlock}
    </div>`;
}

function renderImageGenerationProgress(job) {
  const slot = document.getElementById('image-generation-progress-slot');
  if (!slot || !job) return;
  slot.innerHTML = buildImageGenerationProgressMarkup(job);
}

function setImageRewriteBusy(isBusy) {
  const regenerateBtn = document.querySelector('#image-prompt-modal [data-action="regenerate"]');
  [regenerateBtn].filter(Boolean).forEach(btn => {
    btn.disabled = isBusy;
    btn.classList.toggle('rewrite-loading', isBusy);
  });
  if (regenerateBtn) {
    regenerateBtn.innerHTML = isBusy && imageRewriteMode === 'regenerate'
      ? '<span class="inline-spinner" aria-hidden="true"></span><span>再生成中...</span>'
      : '本文から再生成';
  }
}

function renderImageRewriteProgress(job) {
  const slot = document.getElementById('image-rewrite-progress-slot');
  if (!slot || !job) return;
  const actionLabel = '画像プロンプト再生成';
  const progress = Math.max(0, Math.min(100, Number(job.progress || 0)));
  const label = job.message || (
    job.status === 'completed'
      ? `${actionLabel}完了。画面へ反映しました。`
      : job.status === 'cancelled'
        ? `${actionLabel}を停止しました。`
      : job.status === 'failed'
        ? `${actionLabel}に失敗しました。`
        : `${actionLabel}中です。`
  );
  const canCancel = job.job_id && !['completed', 'failed', 'cancelled'].includes(job.status);
  const cancelButton = canCancel
    ? '<button class="image-generation-copy-btn image-rewrite-cancel-btn" onclick="cancelImageRewrite()">停止してやり直す</button>'
    : '';
  const logs = Array.isArray(job.logs) ? job.logs.slice(-12) : [];
  const logBlock = logs.length
    ? `<div class="image-generation-log">
        ${logs.map(log => `
          <div class="image-generation-log-row ${escAttr(log.level || 'info')}">
            <span class="image-generation-log-time">${esc(log.at || '')}</span>
            <span class="image-generation-log-message">${esc(log.message || '')}</span>
          </div>`).join('')}
      </div>`
    : '';
  slot.innerHTML = `
    <div class="image-generation-progress image-rewrite-progress ${job.status === 'failed' ? 'failed' : ''} ${job.status === 'completed' ? 'completed' : ''} ${job.status === 'cancelled' ? 'cancelled' : ''}">
      <div class="image-generation-progress-top">
        <span>${esc(label)}</span>
        <span>${progress}%</span>
      </div>
      <div class="image-generation-bar"><div style="width:${progress}%"></div></div>
      ${cancelButton}
      ${logBlock}
    </div>`;
}

function applyRewrittenImagePrompt(draftId, rewrittenPrompt, imagePrompt = null) {
  if (!draftId || !rewrittenPrompt) return;
  const nextPrompt = imagePrompt || {
    ...(currentDraft?.image_prompts?.[0] || {}),
    position: currentDraft?.image_prompts?.[0]?.position || 1,
    copy: document.getElementById('image-copy-input')?.value?.trim() || currentDraft?.image_prompts?.[0]?.copy || '',
    prompt: rewrittenPrompt,
  };
  const applyToDraft = draft => {
    if (!draft || draft.draft_id !== draftId) return;
    const prompts = Array.isArray(draft.image_prompts) ? [...draft.image_prompts] : [];
    prompts[0] = { ...(prompts[0] || {}), ...nextPrompt, prompt: rewrittenPrompt };
    draft.image_prompts = prompts;
  };
  applyToDraft(currentDraft);
  const item = allDrafts.find(d => d.draft_id === draftId);
  applyToDraft(item);
  if (currentDraft?.draft_id === draftId) {
    renderDraftList();
    renderPreview(currentDraft);
  } else {
    renderDraftList();
  }
}

function buildImageGenerationRequest(copy, prompt, options = {}) {
  const characterReferenceUrl = getCharacterReferenceUrl();
  const traits = getCharacterTraits();
  const negative = getCharacterNegative();
  const placement = getCharacterPlacement();
  const lines = [
    'Codex/ChatGPTサブスク内の画像生成で、次のX投稿用図解を1枚生成してください。',
    'APIは使わないでください。',
    '添付されたプロフィール画像をキャラクター参照として使い、同じキャラクターの外見を図解内に入れてください。',
    `重要特徴: ${traits}`,
    `禁止する見た目: ${negative}`,
    `配置: ${placement}`,
    '',
    `[CHARACTER REFERENCE]\n${characterReferenceUrl || '(なし)'}`,
    '',
    `[COPY]\n${copy || '(なし)'}`,
    '',
    `[IMAGE PROMPT]\n${prompt}`,
  ];
  if (options.currentImageUrl || options.feedback || options.referenceImageUrl || options.referenceImagePath) {
    lines.push(
      '',
      '[REGENERATION CONTEXT]',
      `現在の生成画像: ${options.currentImageUrl || '(なし)'}`,
      `参照画像URL: ${options.referenceImageUrl || '(なし)'}`,
      `参照画像ローカルパス: ${options.referenceImagePath || '(なし)'}`,
      '',
      `[USER FEEDBACK]\n${options.feedback || '(なし)'}`,
      '',
      '現在の生成画像の良い部分は保ち、ユーザーのフィードバックと参照画像を優先して再生成してください。'
    );
  }
  return lines.join('\n');
}

async function pollImageGeneration() {
  const draftId = imageGenerationDraftId || currentDraft?.draft_id;
  const jobId = imageGenerationJobId || imageGenerationJob?.job_id;
  if (!jobId || !draftId) return;
  try {
    const res = await apiFetch(`${API}/image-generation/status?id=${encodeURIComponent(jobId)}`);
    const data = await res.json();
    if (!data.ok) throw new Error(data.error || '不明');
    imageGenerationJob = { ...data, job_id: data.job_id || jobId };
    imageGenerationJobId = imageGenerationJob.job_id;
    renderImageGenerationProgress(imageGenerationJob);

    if (data.status === 'failed') {
      clearInterval(imageGenerationTimer);
      imageGenerationTimer = null;
      imageGenerationJobId = null;
      pendingAutoPostAfterImage = null;
      showToast(`画像生成失敗: ${data.message || '不明'}`, true);
      return;
    }
    if (data.status === 'cancelled') {
      clearInterval(imageGenerationTimer);
      imageGenerationTimer = null;
      imageGenerationJobId = null;
      pendingAutoPostAfterImage = null;
      showToast('画像生成を停止しました');
      return;
    }
    if (data.status === 'completed') {
      clearInterval(imageGenerationTimer);
      imageGenerationTimer = null;
      imageGenerationJobId = null;
      const applied = applyGeneratedImageUrlToDraft(draftId, data.image_url || imageGenerationJob.image_url || '');
      if (applied) {
        imageGenerationJob = {
          ...imageGenerationJob,
          status: 'completed',
          progress: 100,
          message: '画像生成完了。プレビューに反映しました。',
        };
        renderImageGenerationProgress(imageGenerationJob);
        showToast('✓ 画像をプレビューに反映しました');
      }
    }
  } catch (e) {
    clearInterval(imageGenerationTimer);
    imageGenerationTimer = null;
    imageGenerationJobId = null;
    showToast(`画像生成確認失敗: ${e.message}`, true);
  }
}

function applyGeneratedImageUrlToDraft(draftId, imageUrl) {
  if (!draftId || !imageUrl) return false;
  const applyToDraft = draft => {
    if (!draft || draft.draft_id !== draftId) return false;
    const parts = Array.isArray(draft.parts) ? [...draft.parts] : [];
    if (!parts.length) return false;
    parts[0] = { ...parts[0], image_url: imageUrl };
    draft.parts = parts;
    draft.has_image = true;
    return true;
  };
  const appliedCurrent = applyToDraft(currentDraft);
  const item = allDrafts.find(d => d.draft_id === draftId);
  const appliedItem = applyToDraft(item);
  if (appliedCurrent) {
    renderDraftList();
    renderPreview(currentDraft);
  } else if (appliedItem) {
    renderDraftList();
  }
  return appliedCurrent || appliedItem;
}

async function refreshDraftAfterImageGeneration(draftId, forceRender = false) {
  const reloadRes = await apiFetch(`${API}/draft?id=${encodeURIComponent(draftId)}`);
  const draft = await reloadRes.json();
  const imageAttached = !!draft.parts?.some(part => (part.image_url || '').trim());
  const jobImageUrl = imageGenerationJob?.image_url || '';
  if (!imageAttached && jobImageUrl) {
    return applyGeneratedImageUrlToDraft(draftId, jobImageUrl);
  }
  if (!imageAttached) return false;

  const item = allDrafts.find(d => d.draft_id === draft.draft_id);
  if (item) {
    item.parts = draft.parts;
    item.has_image = imageAttached;
    item.status = draft.status;
    item.memo = draft.memo;
  }
  if (currentDraft?.draft_id === draft.draft_id) {
    currentDraft = draft;
    renderDraftList();
    renderPreview(currentDraft);
  } else {
    renderDraftList();
  }
  return imageAttached;
}

async function cancelImageGeneration() {
  const jobId = imageGenerationJobId || imageGenerationJob?.job_id;
  if (!jobId) {
    showToast('停止する画像生成がありません');
    return;
  }
  try {
    const res = await apiFetch(`${API}/image-generation/cancel`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ job_id: jobId }),
    });
    const data = await res.json();
    if (!data.ok) throw new Error(data.error || '不明');
    imageGenerationJob = { ...data, job_id: data.job_id || jobId };
    imageGenerationJobId = null;
    renderImageGenerationProgress(imageGenerationJob);
    clearInterval(imageGenerationTimer);
    imageGenerationTimer = null;
    imageGenerationDraftId = null;
    pendingAutoPostAfterImage = null;
    showToast('画像生成を停止しました');
  } catch (e) {
    showToast(`画像生成の停止に失敗: ${e.message}`, true);
  }
}

async function pollImageRewrite() {
  if (!imageRewriteJob?.job_id) return;
  const jobId = imageRewriteJob.job_id;
  try {
    const res = await apiFetch(`${API}/image-prompt/rewrite/status?id=${encodeURIComponent(jobId)}`);
    const data = await res.json();
    if (!data.ok) throw new Error(data.error || '不明');
    imageRewriteJob = { ...data, job_id: data.job_id || jobId };
    imageRewriteMode = data.mode || imageRewriteMode;
    const labels = imagePromptRewriteLabels(imageRewriteMode);
    renderImageRewriteProgress(imageRewriteJob);
    if (data.status === 'completed') {
      clearInterval(imageRewriteTimer);
      imageRewriteTimer = null;
      const finishedJob = { ...imageRewriteJob, status: 'completed', progress: 100 };
      imageRewriteJob = null; // ポーリング停止を確実にする

      const draftId = data.draft_id || imageRewriteDraftId;
      const rewrittenPrompt = (data.prompt || '').trim();
      
      if (rewrittenPrompt) {
        const textarea = document.getElementById('image-prompt-textarea');
        if (textarea) textarea.value = rewrittenPrompt;
      }
      
      if (draftId && rewrittenPrompt) {
        applyRewrittenImagePrompt(draftId, rewrittenPrompt, data.image_prompt || null);
      }
      
      if (draftId) await refreshDraftAfterImageGeneration(draftId, true);
      
      setImageRewriteBusy(false);
      const helpEl = document.getElementById('image-modal-help');
      if (helpEl) {
        helpEl.textContent = data.changed === false
          ? `${labels.label}完了。ただし結果は元の内容と同じでした。指示を具体化して再実行してください。`
          : `${labels.label}完了。結果は保存済みで、プレビューにも反映しました。`;
      }
      imageRewriteDraftId = null;
      showToast(data.account_memory_error
        ? `✓ ${labels.label}完了。今後の注意保存は失敗: ${data.account_memory_error}`
        : data.account_memory_path
          ? `✓ ${labels.label}完了。今後の注意もアカウント情報に保存しました`
          : data.changed === false
            ? `${labels.label}完了。ただし変更はありませんでした`
            : `✓ ${labels.label}完了。保存して画面を更新しました`);
    } else if (data.status === 'cancelled') {
      clearInterval(imageRewriteTimer);
      imageRewriteTimer = null;
      imageRewriteJob = null;
      setImageRewriteBusy(false);
      imageRewriteDraftId = null;
      const helpEl = document.getElementById('image-modal-help');
      if (helpEl) helpEl.textContent = `${labels.label}を停止しました。指示を書き直して再実行できます。`;
      showToast(`${labels.label}を停止しました`);
    } else if (data.status === 'failed') {
      clearInterval(imageRewriteTimer);
      imageRewriteTimer = null;
      imageRewriteJob = null;
      setImageRewriteBusy(false);
      imageRewriteDraftId = null;
      const helpEl = document.getElementById('image-modal-help');
      if (helpEl) helpEl.textContent = `${labels.label}に失敗しました。ログを確認してください。`;
      showToast(`${labels.failPrefix}: ${data.message || '不明'}`, true);
    }
  } catch (e) {
    clearInterval(imageRewriteTimer);
    imageRewriteTimer = null;
    setImageRewriteBusy(false);
    imageRewriteDraftId = null;
    showToast(`${imagePromptRewriteLabels(imageRewriteMode).label}確認失敗: ${e.message}`, true);
  }
}

async function cancelImageRewrite() {
  if (!imageRewriteJob?.job_id) {
    showToast('停止する処理がありません');
    return;
  }
  const jobId = imageRewriteJob.job_id;
  const actionLabel = '画像プロンプト再生成';
  try {
    const res = await apiFetch(`${API}/image-prompt/rewrite/cancel`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ job_id: jobId }),
    });
    const data = await res.json();
    if (!data.ok) throw new Error(data.error || '不明');
    imageRewriteJob = { ...data, job_id: data.job_id || jobId };
    renderImageRewriteProgress(imageRewriteJob);
    clearInterval(imageRewriteTimer);
    imageRewriteTimer = null;
    setImageRewriteBusy(false);
    imageRewriteDraftId = null;
    const helpEl = document.getElementById('image-modal-help');
    if (helpEl) helpEl.textContent = `${actionLabel}を停止しました。指示を書き直して再実行できます。`;
    showToast(`${actionLabel}を停止しました`);
  } catch (e) {
    showToast(`停止失敗: ${e.message}`, true);
  }
}

function currentDraftTextForImagePrompt() {
  return (currentDraft?.parts || [])
    .map(part => (part.content || '').trim())
    .filter(Boolean)
    .join('\n\n');
}

function imagePromptRewriteLabels(mode) {
  return {
    label: '画像プロンプト再生成',
    running: '画像プロンプト再生成中です。止める場合は進捗欄の「停止してやり直す」を押してください',
    help: '画像プロンプト再生成中です。進捗とログを下に表示します。完了したら自動保存して画面を更新します。',
    starting: '画像プロンプト再生成を開始しています',
    toastStart: '画像プロンプト再生成を開始しました',
    failPrefix: '再生成失敗',
  };
}

async function startImagePromptRewriteJob(mode = 'regenerate') {
  if (!currentDraft) return;
  if (mode !== 'regenerate') {
    showToast('画像プロンプトのAIリライトは無効です。画像の再生成または画像プロンプト再生成を使ってください', true);
    return;
  }
  if (imageRewriteTimer) {
    showToast(imagePromptRewriteLabels(imageRewriteMode).running);
    return;
  }
  const overlay = document.getElementById('image-prompt-modal');
  const isOpen = overlay && overlay.style.display !== 'none';
  if (!isOpen || imagePromptModalMode !== mode) {
    openImagePromptModal(mode);
  }
  imageRewriteMode = mode;
  const { copy, prompt, instruction } = getImageModalValues();
  if (!currentDraftTextForImagePrompt()) {
    showToast('下書き本文がないため、画像プロンプトを再生成できません', true);
    return;
  }
  const draftId = currentDraft.draft_id;
  const labels = imagePromptRewriteLabels(mode);
  setImageRewriteBusy(true);
  const helpEl = document.getElementById('image-modal-help');
  if (helpEl) helpEl.textContent = labels.help;
  imageRewriteJob = {
    mode,
    status: 'starting',
    progress: 3,
    message: labels.starting,
    logs: [{ at: new Date().toLocaleTimeString('ja-JP', { hour12: false }), level: 'info', message: 'ブラウザからジョブを送信します' }],
  };
  imageRewriteDraftId = draftId;
  renderImageRewriteProgress(imageRewriteJob);
  showToast(labels.toastStart);
  try {
    const res = await apiFetch(`${API}/image-prompt/rewrite/start`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        mode,
        draft_id: draftId,
        copy,
        prompt,
        instruction,
        character_reference_url: getCharacterReferenceUrl(),
        remember_rewrite_preference: false,
        selected_logo_names: selectedLogoNamesForGeneration(draftId),
      }),
    });
    const data = await res.json();
    if (!data.ok) throw new Error(data.error || '不明');
    imageRewriteJob = data;
    renderImageRewriteProgress(data);
    imageRewriteTimer = setInterval(pollImageRewrite, 1500);
    await pollImageRewrite();
  } catch (e) {
    setImageRewriteBusy(false);
    imageRewriteDraftId = null;
    imageRewriteJob = {
      mode,
      status: 'failed',
      progress: 100,
      message: e.message,
      logs: [{ at: new Date().toLocaleTimeString('ja-JP', { hour12: false }), level: 'error', message: e.message }],
    };
    renderImageRewriteProgress(imageRewriteJob);
    showToast(`${labels.failPrefix}: ${e.message}`, true);
  }
}

async function rewriteImagePrompt() {
  showToast('画像プロンプトのAIリライトは無効です。画像の再生成または画像プロンプト再生成を使ってください', true);
}

async function regenerateImagePrompt() {
  return startImagePromptRewriteJob('regenerate');
}

async function saveImagePrompt() {
  if (!currentDraft) return;
  const { copy, prompt } = getImageModalValues();
  if (!prompt) { showToast('画像プロンプトがありません', true); return; }
  try {
    const res = await apiFetch(`${API}/image-prompt`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        draft_id: currentDraft.draft_id,
        copy,
        prompt,
      }),
    });
    const data = await res.json();
    if (!data.ok) throw new Error(data.error || '不明');
    currentDraft.image_prompts = [data.image_prompt];
    renderPreview(currentDraft);
    showToast('✓ 画像プロンプトを保存しました');
  } catch (e) {
    showToast(`保存失敗: ${e.message}`, true);
  }
}

function ensureTextRewriteModal() {
  let overlay = document.getElementById('text-rewrite-modal');
  if (overlay) return overlay;

  overlay = document.createElement('div');
  overlay.id = 'text-rewrite-modal';
  overlay.className = 'modal-overlay';
  overlay.style.display = 'none';
  overlay.innerHTML = `
    <div class="modal image-modal text-rewrite-modal" role="dialog" aria-modal="true" aria-labelledby="text-rewrite-title" onclick="event.stopPropagation()">
      <div class="thread-modal-header">
        <div>
          <div class="thread-step-label" id="text-rewrite-scope-label">投稿本文</div>
          <h3 class="image-modal-title" id="text-rewrite-title">本文AI修正</h3>
        </div>
        <button class="thread-close-btn" onclick="closeTextRewriteModal()" aria-label="閉じる">${materialIcon('close')}</button>
      </div>
      <div class="image-modal-field">
        <label class="image-modal-label" id="text-rewrite-current-label">現在の本文</label>
        <div class="thread-part-content text-rewrite-current" id="text-rewrite-current"></div>
      </div>
      <div class="image-modal-field">
        <label class="image-modal-label">修正指示</label>
        <textarea class="image-modal-instruction" id="text-rewrite-instruction" rows="5"
          placeholder="例: もっと自然な口調に。結論を先にして、煽りすぎず読みやすく"></textarea>
      </div>
      <label class="image-modal-checkbox">
        <input id="remember-text-rule" type="checkbox">
        <span>今後もこのアカウントの本文では同じ方針で気をつける</span>
      </label>
      <p class="image-modal-help" id="text-rewrite-help">画像プロンプトは変更しません。選択した投稿本文だけを修正します。</p>
      <div class="image-modal-actions">
        <button class="cancel-btn" onclick="closeTextRewriteModal()">閉じる</button>
        <button class="save-btn" id="text-rewrite-start-btn" onclick="rewriteDraftText()">${materialIcon('auto_fix_high', { filled: true })}本文だけ修正</button>
      </div>
    </div>`;
  overlay.onclick = closeTextRewriteModal;
  document.body.appendChild(overlay);
  return overlay;
}

function openTextRewriteModal(partId) {
  const part = currentDraft?.parts?.find(p => p.part_id === partId);
  if (!part) return;
  textRewriteMode = 'part';
  textRewriteTargetPartId = partId;
  const overlay = ensureTextRewriteModal();
  document.getElementById('text-rewrite-scope-label').textContent = `投稿 ${part.position || ''}`;
  document.getElementById('text-rewrite-title').textContent = '本文AI修正';
  document.getElementById('text-rewrite-current-label').textContent = '現在の本文';
  document.getElementById('text-rewrite-current').textContent = part.content || '';
  document.getElementById('text-rewrite-instruction').value = '';
  document.getElementById('remember-text-rule').checked = false;
  document.getElementById('text-rewrite-help').textContent = '画像プロンプトは変更しません。選択した投稿本文だけを修正します。';
  setTextRewriteBusy(false);
  overlay.style.display = 'flex';
}

function openDraftTextRewriteModal() {
  if (!currentDraft?.parts?.length) return;
  textRewriteMode = 'draft';
  textRewriteTargetPartId = null;
  const overlay = ensureTextRewriteModal();
  const wholeText = currentDraft.parts.map(part => {
    const position = part.position || '';
    return `投稿 ${position}\n${part.content || ''}`;
  }).join('\n\n---\n\n');
  document.getElementById('text-rewrite-scope-label').textContent = '下書き全体';
  document.getElementById('text-rewrite-title').textContent = '全文AI修正';
  document.getElementById('text-rewrite-current-label').textContent = '下書き全文';
  document.getElementById('text-rewrite-current').textContent = wholeText;
  document.getElementById('text-rewrite-instruction').value = '';
  document.getElementById('remember-text-rule').checked = false;
  document.getElementById('text-rewrite-help').textContent = '画像プロンプトは変更しません。全投稿の流れを見ながら本文全体を修正します。';
  setTextRewriteBusy(false);
  overlay.style.display = 'flex';
}

function closeTextRewriteModal(force = false) {
  if (textRewriteBusy && !force) return;
  const overlay = document.getElementById('text-rewrite-modal');
  if (overlay) overlay.style.display = 'none';
  textRewriteTargetPartId = null;
  textRewriteMode = 'part';
}

function setTextRewriteBusy(isBusy) {
  textRewriteBusy = isBusy;
  const btn = document.getElementById('text-rewrite-start-btn');
  if (!btn) return;
  btn.disabled = isBusy;
  btn.classList.toggle('rewrite-loading', isBusy);
  btn.innerHTML = isBusy
    ? '<span class="inline-spinner" aria-hidden="true"></span><span>本文修正中...</span>'
    : textRewriteMode === 'draft'
      ? `${materialIcon('auto_fix_high', { filled: true })}全文を修正`
      : `${materialIcon('auto_fix_high', { filled: true })}この投稿だけ修正`;
}

function applyTextRewriteToDraft(partId, content) {
  const applyToDraft = draft => {
    if (!draft?.parts) return false;
    const part = draft.parts.find(p => p.part_id === partId);
    if (!part) return false;
    part.content = content;
    return true;
  };
  const changedCurrent = applyToDraft(currentDraft);
  const listDraft = allDrafts.find(d => d.draft_id === currentDraft?.draft_id);
  if (listDraft) {
    if (!applyToDraft(listDraft)) {
      listDraft.preview_content = content;
    }
  }
  if (changedCurrent) {
    renderDraftList();
    renderPreview(currentDraft);
  } else {
    renderDraftList();
  }
}

function applyTextRewritePartsToDraft(parts) {
  const normalizedParts = Array.isArray(parts) ? parts : [];
  const contentByPartId = new Map(normalizedParts.map(part => [String(part.part_id), part.content || '']));
  const applyToDraft = draft => {
    if (!draft?.parts) return false;
    let changed = false;
    draft.parts.forEach(part => {
      const nextContent = contentByPartId.get(String(part.part_id));
      if (nextContent !== undefined) {
        part.content = nextContent;
        changed = true;
      }
    });
    return changed;
  };
  const changedCurrent = applyToDraft(currentDraft);
  const listDraft = allDrafts.find(d => d.draft_id === currentDraft?.draft_id);
  if (listDraft) {
    applyToDraft(listDraft);
    const firstPart = normalizedParts.find(part => Number(part.position || 0) === 1) || normalizedParts[0];
    if (firstPart) listDraft.preview_content = firstPart.content || '';
  }
  renderDraftList();
  if (changedCurrent) renderPreview(currentDraft);
}

async function rewriteDraftTextPart() {
  if (!currentDraft || !textRewriteTargetPartId || textRewriteBusy) return;
  const part = currentDraft.parts.find(p => p.part_id === textRewriteTargetPartId);
  if (!part) return;
  const instruction = document.getElementById('text-rewrite-instruction')?.value?.trim() || '';
  if (!instruction) {
    showToast('本文の修正指示を入力してください', true);
    return;
  }
  const rememberRule = !!document.getElementById('remember-text-rule')?.checked;
  const helpEl = document.getElementById('text-rewrite-help');
  try {
    setTextRewriteBusy(true);
    if (helpEl) helpEl.textContent = '本文だけを修正中です。画像プロンプトは変更しません。';
    const res = await apiFetch(`${API}/draft/text-rewrite`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        draft_id: currentDraft.draft_id,
        part_id: textRewriteTargetPartId,
        instruction,
        remember_rule: rememberRule,
      }),
    });
    const data = await readApiJson(res, '本文AI修正API');
    if (!data.ok) throw new Error(data.error || '不明');
    applyTextRewriteToDraft(textRewriteTargetPartId, data.content || '');
    setTextRewriteBusy(false);
    closeTextRewriteModal(true);
    showToast(data.account_rule_error
      ? `✓ 本文を修正しました。ルール追記は失敗: ${data.account_rule_error}`
      : data.account_rule_path
        ? '✓ 本文を修正し、アカウントルールにも追記しました'
        : '✓ 本文を修正しました');
  } catch (e) {
    if (helpEl) helpEl.textContent = `本文AI修正に失敗しました: ${e.message}`;
    showToast(`本文AI修正失敗: ${e.message}`, true);
  } finally {
    setTextRewriteBusy(false);
  }
}

async function rewriteDraftTextAll() {
  if (!currentDraft || textRewriteBusy) return;
  const instruction = document.getElementById('text-rewrite-instruction')?.value?.trim() || '';
  if (!instruction) {
    showToast('本文の修正指示を入力してください', true);
    return;
  }
  const rememberRule = !!document.getElementById('remember-text-rule')?.checked;
  const helpEl = document.getElementById('text-rewrite-help');
  try {
    setTextRewriteBusy(true);
    if (helpEl) helpEl.textContent = '全文を修正中です。画像プロンプトは変更しません。';
    const res = await apiFetch(`${API}/draft/text-rewrite-all`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        draft_id: currentDraft.draft_id,
        instruction,
        remember_rule: rememberRule,
      }),
    });
    const data = await readApiJson(res, '全文AI修正API');
    if (!data.ok) throw new Error(data.error || '不明');
    applyTextRewritePartsToDraft(data.parts || []);
    setTextRewriteBusy(false);
    closeTextRewriteModal(true);
    showToast(data.account_rule_error
      ? `✓ 全文を修正しました。ルール追記は失敗: ${data.account_rule_error}`
      : data.account_rule_path
        ? '✓ 全文を修正し、アカウントルールにも追記しました'
        : '✓ 全文を修正しました');
  } catch (e) {
    if (helpEl) helpEl.textContent = `全文AI修正に失敗しました: ${e.message}`;
    showToast(`全文AI修正失敗: ${e.message}`, true);
  } finally {
    setTextRewriteBusy(false);
  }
}

async function rewriteDraftText() {
  if (textRewriteMode === 'draft') {
    return rewriteDraftTextAll();
  }
  return rewriteDraftTextPart();
}

function ensureCharacterSettingsModal() {
  let overlay = document.getElementById('character-settings-modal');
  if (overlay) return overlay;

  overlay = document.createElement('div');
  overlay.id = 'character-settings-modal';
  overlay.className = 'modal-overlay';
  overlay.style.display = 'none';
  overlay.innerHTML = `
    <div class="modal image-modal character-modal" role="dialog" aria-modal="true" aria-labelledby="character-modal-title" onclick="event.stopPropagation()">
      <div class="thread-modal-header">
        <div>
          <div class="thread-step-label">アカウント別に保存</div>
          <h3 class="image-modal-title" id="character-modal-title">キャラクター設定</h3>
        </div>
        <button class="thread-close-btn" onclick="closeCharacterSettingsModal()" aria-label="閉じる">${materialIcon('close')}</button>
      </div>
      <div class="character-preview-row">
        <img id="character-reference-preview" class="character-reference-preview" alt="参照画像">
        <div class="character-preview-meta">
          <div id="character-account-label" class="thread-step-label"></div>
          <p class="image-modal-help">この設定は保存され、サーバー再起動後も画像生成で使われます。</p>
        </div>
      </div>
      <label class="image-modal-label">参照画像URL</label>
      <input class="image-modal-input" id="character-reference-url-input" type="url"
        placeholder="https://pbs.twimg.com/profile_images/...jpg">
      <label class="image-modal-label">キャラクターの重要特徴</label>
      <textarea class="image-modal-textarea" id="character-traits-input" rows="4"></textarea>
      <label class="image-modal-label">禁止する見た目</label>
      <textarea class="image-modal-instruction" id="character-negative-input" rows="3"></textarea>
      <label class="image-modal-label">配置ルール</label>
      <input class="image-modal-input" id="character-placement-input" type="text">
      <div class="image-modal-actions">
        <button class="cancel-btn" onclick="closeCharacterSettingsModal()">閉じる</button>
        <button class="save-btn" onclick="saveCharacterSettings()">保存</button>
      </div>
    </div>`;
  overlay.onclick = closeCharacterSettingsModal;
  document.body.appendChild(overlay);
  return overlay;
}

async function openCharacterSettingsModal() {
  if (!currentDraft) return;
  const overlay = ensureCharacterSettingsModal();
  const accountKey = currentDraft.x_username || currentDraft.draft_id;
  try {
    const res = await apiFetch(`${API}/character-settings?account=${encodeURIComponent(accountKey)}&fallback_url=${encodeURIComponent(currentDraft.profile_image_url || '')}`);
    const data = await res.json();
    if (!data.ok) throw new Error(data.error || '不明');
    currentDraft.character_setting = data.setting;
  } catch (e) {
    showToast(`キャラ設定取得失敗: ${e.message}`, true);
  }
  const setting = getCharacterSetting();
  document.getElementById('character-account-label').textContent = `@${accountKey}`;
  document.getElementById('character-reference-url-input').value = setting.reference_url || '';
  document.getElementById('character-traits-input').value = setting.traits || '';
  document.getElementById('character-negative-input').value = setting.negative || '';
  document.getElementById('character-placement-input').value = setting.placement || '';
  const preview = document.getElementById('character-reference-preview');
  preview.src = setting.reference_url || currentDraft.profile_image_url || '';
  overlay.style.display = 'flex';
}

function closeCharacterSettingsModal() {
  const overlay = document.getElementById('character-settings-modal');
  if (overlay) overlay.style.display = 'none';
}

function currentDraftAvatarMarkup() {
  if (!currentDraft) return '<span class="settings-shortcut-avatar">𝕏</span>';
  const label = currentDraft.display_name || currentDraft.x_username || 'account';
  if (currentDraft.profile_image_url) {
    return `<span class="settings-shortcut-avatar"><img src="${escAttr(currentDraft.profile_image_url)}" alt="${escAttr(label)}" loading="lazy"></span>`;
  }
  const initial = String(label).charAt(0).toUpperCase() || 'X';
  return `<span class="settings-shortcut-avatar">${esc(initial)}</span>`;
}

async function saveCharacterSettings() {
  if (!currentDraft) return;
  const accountKey = currentDraft.x_username || currentDraft.draft_id;
  const payload = {
    account_key: accountKey,
    reference_url: document.getElementById('character-reference-url-input')?.value?.trim() || '',
    traits: document.getElementById('character-traits-input')?.value?.trim() || '',
    negative: document.getElementById('character-negative-input')?.value?.trim() || '',
    placement: document.getElementById('character-placement-input')?.value?.trim() || '',
  };
  try {
    const res = await apiFetch(`${API}/character-settings`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (!data.ok) throw new Error(data.error || '不明');
    currentDraft.character_setting = data.setting;
    const first = currentDraft.image_prompts?.[0];
    if (first) first.character_reference_url = data.setting.reference_url;
    showToast('✓ キャラクター設定を保存しました');
    closeCharacterSettingsModal();
    renderPreview(currentDraft);
  } catch (e) {
    showToast(`キャラ設定保存失敗: ${e.message}`, true);
  }
}

function ensureEnvSettingsModal() {
  let overlay = document.getElementById('env-settings-modal');
  if (overlay) return overlay;
  overlay = document.createElement('div');
  overlay.id = 'env-settings-modal';
  overlay.className = 'modal-overlay';
  overlay.style.display = 'none';
  overlay.innerHTML = `
    <div class="modal env-modal" role="dialog" aria-modal="true" aria-labelledby="env-modal-title" onclick="event.stopPropagation()">
      <div class="thread-modal-header">
        <div>
          <div class="thread-step-label" id="env-settings-source">設定</div>
          <h3 class="image-modal-title" id="env-modal-title">認証・連携設定</h3>
        </div>
        <button class="thread-close-btn" onclick="closeEnvSettingsModal()" aria-label="閉じる">${materialIcon('close')}</button>
      </div>
      <p class="image-modal-help">Googleログイン中はユーザー別DB設定へ保存します。未ログイン時だけローカル .env へ保存します。</p>
      <div class="settings-shortcuts">
        <button class="settings-shortcut-btn" onclick="openCharacterSettingsFromEnvModal()" ${currentDraft ? '' : 'disabled'}>
          <span id="settings-character-avatar-slot">${currentDraftAvatarMarkup()}</span>
          <strong>キャラ設定</strong>
          <small>${currentDraft ? `@${esc(currentDraft.x_username || '')}` : '下書きを選択すると使えます'}</small>
        </button>
      </div>
      <div id="env-settings-error" class="error-msg" style="display:none"></div>
      <div id="env-settings-list" class="env-settings-list">
        <div class="loading">読み込み中...</div>
      </div>
      <div class="env-add-row">
        <input id="env-new-key" class="image-modal-input" type="text" placeholder="X_... / NEON_... / DISCORD_WEBHOOK_X_DRAFT">
        <input id="env-new-value" class="image-modal-input" type="password" placeholder="値">
      </div>
      <div class="image-modal-actions">
        <button class="cancel-btn" onclick="closeEnvSettingsModal()">閉じる</button>
        <button class="save-btn" onclick="saveEnvSettings()">保存</button>
      </div>
    </div>`;
  overlay.onclick = closeEnvSettingsModal;
  document.body.appendChild(overlay);
  return overlay;
}

async function openEnvSettingsModal() {
  const overlay = ensureEnvSettingsModal();
  const shortcut = overlay.querySelector('.settings-shortcut-btn');
  if (shortcut) {
    shortcut.disabled = !currentDraft;
    const small = shortcut.querySelector('small');
    if (small) small.textContent = currentDraft ? `@${currentDraft.x_username || ''}` : '下書きを選択すると使えます';
    const avatarSlot = shortcut.querySelector('#settings-character-avatar-slot');
    if (avatarSlot) avatarSlot.innerHTML = currentDraftAvatarMarkup();
  }
  overlay.style.display = 'flex';
  await loadEnvSettings();
}

function openCharacterSettingsFromEnvModal() {
  if (!currentDraft) {
    showToast('先に下書きを選択してください', true);
    return;
  }
  closeEnvSettingsModal();
  openCharacterSettingsModal();
}

function closeEnvSettingsModal() {
  const overlay = document.getElementById('env-settings-modal');
  if (overlay) overlay.style.display = 'none';
}

function isEditableEnvKey(key) {
  return key === 'DISCORD_WEBHOOK_X_DRAFT' || key === 'NEON_DATABASE_URL' || key.startsWith('X_') || key.startsWith('NEON_');
}

async function loadEnvSettings() {
  const list = document.getElementById('env-settings-list');
  const error = document.getElementById('env-settings-error');
  if (!list || !error) return;
  error.style.display = 'none';
  list.innerHTML = '<div class="loading">読み込み中...</div>';
  try {
    const res = await apiFetch(`${API}/env-settings`);
    const data = await res.json();
    if (!res.ok || !data.ok) throw new Error(data.error || '取得できませんでした');
    const items = Array.isArray(data.items) ? data.items : [];
    const source = document.getElementById('env-settings-source');
    if (source) source.textContent = data.storage === 'user_settings' ? 'ユーザー別DB設定' : data.path || '.env';
    if (items.length === 0) {
      list.innerHTML = '<div class="loading">設定がありません</div>';
      return;
    }
    list.innerHTML = items.map(item => `
      <label class="env-row">
          <span class="env-row-key">
            <span>${esc(item.key)}</span>
          <span>${item.source === 'user_settings' ? 'ユーザー別' : 'fallback'} ・ ${item.is_set ? esc(item.masked || 'set') : '未設定'}</span>
        </span>
        <input class="image-modal-input env-input" data-env-key="${escAttr(item.key)}"
          type="${item.sensitive ? 'password' : 'text'}"
          value="${escAttr(item.value || '')}"
          autocomplete="off">
      </label>`).join('');
  } catch (e) {
    list.innerHTML = '';
    error.textContent = e.message;
    error.style.display = 'block';
  }
}

async function saveEnvSettings() {
  const updates = {};
  document.querySelectorAll('.env-input').forEach(input => {
    const key = input.dataset.envKey;
    if (key) updates[key] = input.value;
  });
  const newKey = document.getElementById('env-new-key')?.value?.trim() || '';
  const newValue = document.getElementById('env-new-value')?.value || '';
  if (newKey && !isEditableEnvKey(newKey)) {
    showToast('X / Neon / DISCORD_WEBHOOK_X_DRAFT のキーだけ追加できます', true);
    return;
  }
  if (newKey) updates[newKey] = newValue;
  try {
    const res = await apiFetch(`${API}/env-settings`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ updates }),
    });
    const data = await res.json();
    if (!res.ok || !data.ok) throw new Error(data.error || '保存できませんでした');
    showToast(data.storage === 'user_settings' ? '✓ ユーザー別設定を保存しました' : '✓ .env を保存しました');
    document.getElementById('env-new-key').value = '';
    document.getElementById('env-new-value').value = '';
    await loadEnvSettings();
    await loadAccounts();
  } catch (e) {
    showToast(`環境変数保存失敗: ${e.message}`, true);
  }
}

async function attachGeneratedImage() {
  if (!currentDraft) return;
  const { imagePath } = getImageModalValues();
  if (!imagePath) { showToast('画像パスを入力してください', true); return; }
  try {
    const res = await apiFetch(`${API}/draft/image`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        draft_id: currentDraft.draft_id,
        image_path: imagePath,
      }),
    });
    const data = await res.json();
    if (!data.ok) throw new Error(data.error || '不明');

    const reloadRes = await apiFetch(`${API}/draft?id=${currentDraft.draft_id}`);
    currentDraft = await reloadRes.json();
    const item = allDrafts.find(d => d.draft_id === currentDraft.draft_id);
    if (item) {
      item.parts = currentDraft.parts;
      item.has_image = true;
    }
    renderDraftList();
    renderPreview(currentDraft);
    showToast('✓ 1投稿目の投稿画像に設定しました');
  } catch (e) {
    showToast(`画像添付失敗: ${e.message}`, true);
  }
}

async function detachDraftImage(position = 1) {
  if (!currentDraft) return;
  const choice = await showChoiceModal({
    title: '画像を外しますか？',
    message: '下書きから画像添付だけを外します。生成済み画像ファイルは削除しません。',
    primary: '画像を外す',
    cancel: 'キャンセル',
  });
  if (choice !== 'primary') return;
  try {
    const res = await apiFetch(`${API}/draft/image/delete`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        draft_id: currentDraft.draft_id,
        position,
      }),
    });
    const data = await readApiJson(res, '画像を外すAPI');
    if (!data.ok) throw new Error(data.error || '不明');

    const reloadRes = await apiFetch(`${API}/draft?id=${encodeURIComponent(currentDraft.draft_id)}`);
    currentDraft = await reloadRes.json();
    const item = allDrafts.find(d => d.draft_id === currentDraft.draft_id);
    if (item) {
      item.parts = currentDraft.parts;
      item.has_image = currentDraft.has_image;
    }
    renderDraftList();
    renderPreview(currentDraft);
    showToast('✓ 画像を下書きから外しました');
  } catch (e) {
    showToast(`画像を外せませんでした: ${e.message}`, true);
  }
}

async function copyImagePrompt() {
  const prompts = currentDraft?.image_prompts;
  if (!prompts || prompts.length === 0) return;
  const text = (prompts[0].prompt || '').trim();
  if (!text) { showToast('プロンプトがありません', true); return; }
  try {
    await navigator.clipboard.writeText(text);
    showToast('✓ 画像プロンプトをコピーしました');
  } catch {
    showToast('コピーに失敗しました', true);
  }
}

async function copySourceImageUrl() {
  const imageUrl = currentDraft?.parts?.[0]?.image_url;
  if (!imageUrl) { showToast('元画像URLがありません', true); return; }
  try {
    await navigator.clipboard.writeText(imageUrl);
    showToast('✓ 元画像URLをコピーしました');
  } catch {
    showToast('コピーに失敗しました', true);
  }
}

async function copyGeneratedImage() {
  const imageUrl = currentDraft?.parts?.[0]?.image_url;
  return copyGeneratedImageFromUrl(imageUrl);
}

async function saveGeneratedImage() {
  const imageUrl = currentDraft?.parts?.[0]?.image_url;
  return saveGeneratedImageFromUrl(imageUrl);
}

async function saveGeneratedImageFromUrl(imageUrl) {
  if (!imageUrl) { showToast('画像がありません', true); return; }
  const absoluteUrl = new URL(imageUrl, window.location.href).href;
  const filename = imageUrl.split('?')[0].split('/').pop() || `x-draft-${currentDraft?.draft_id || 'image'}.png`;
  try {
    const res = await fetch(absoluteUrl);
    if (!res.ok) throw new Error('image fetch failed');
    const blob = await res.blob();
    const objectUrl = URL.createObjectURL(blob);
    triggerImageDownload(objectUrl, filename);
    setTimeout(() => URL.revokeObjectURL(objectUrl), 3000);
    showToast('✓ 画像を保存しました');
  } catch {
    triggerImageDownload(absoluteUrl, filename);
    showToast('画像保存を開始しました');
  }
}

function triggerImageDownload(url, filename) {
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  a.rel = 'noopener';
  document.body.appendChild(a);
  a.click();
  a.remove();
}

async function copyGeneratedImageFromUrl(imageUrl) {
  if (!imageUrl) { showToast('画像がありません', true); return; }
  try {
    const res = await fetch(imageUrl);
    if (!res.ok) throw new Error('image fetch failed');
    const blob = await res.blob();
    if (!navigator.clipboard || typeof ClipboardItem === 'undefined') {
      await navigator.clipboard.writeText(new URL(imageUrl, window.location.href).href);
      showToast('画像URLをコピーしました');
      return;
    }
    await navigator.clipboard.write([
      new ClipboardItem({ [blob.type || 'image/png']: blob })
    ]);
    showToast('✓ 画像をコピーしました');
  } catch {
    try {
      await navigator.clipboard.writeText(new URL(imageUrl, window.location.href).href);
      showToast('画像URLをコピーしました');
    } catch {
      showToast('画像コピーに失敗しました', true);
    }
  }
}

// ── リプ追加 ─────────────────────────────────────────

function startAddReply() {
  const zone = document.getElementById('add-reply-zone');
  if (!zone) return;
  zone.innerHTML = `
    <div class="new-reply-area">
      <textarea class="tweet-textarea" id="new-reply-textarea"
        placeholder="リプの内容を入力..." rows="4"></textarea>
      <div class="edit-actions">
        <button class="save-btn" onclick="saveNewPart()">${materialIcon('add', { filled: true })}追加</button>
        <button class="cancel-btn" onclick="cancelAddReply()">キャンセル</button>
      </div>
    </div>`;
  const ta = document.getElementById('new-reply-textarea');
  if (ta) ta.focus();
}

function cancelAddReply() {
  const zone = document.getElementById('add-reply-zone');
  if (zone) zone.innerHTML =
    `<button class="add-reply-btn" onclick="startAddReply()">${materialIcon('add')}リプを追加</button>`;
}

async function saveNewPart() {
  if (!currentDraft) return;
  const ta = document.getElementById('new-reply-textarea');
  const content = ta?.value?.trim();
  if (!content) { showToast('内容を入力してください', true); return; }

  try {
    const res = await apiFetch(`${API}/draft/part`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ draft_id: currentDraft.draft_id, content }),
    });
    const data = await res.json();
    if (!data.ok) throw new Error(data.error || '不明');

    // 再取得して再描画
    const reloadRes = await apiFetch(`${API}/draft?id=${currentDraft.draft_id}`);
    currentDraft    = await reloadRes.json();
    renderPreview(currentDraft);

    // サイドバーのキャッシュも更新
    const d = allDrafts.find(d => d.draft_id === currentDraft.draft_id);
    if (d) d.parts = currentDraft.parts;
    renderDraftList();
    showToast('✓ リプを追加しました');
  } catch (e) {
    showToast(`追加エラー: ${e.message}`, true);
  }
}

// ── モバイル比較トグル ────────────────────────────────

function switchCompareCol(col) {
  const colOrig   = document.getElementById('col-original');
  const colDraft  = document.getElementById('col-draft');
  const btnDraft  = document.getElementById('ctoggle-draft');
  const btnOrig   = document.getElementById('ctoggle-original');
  if (!colOrig || !colDraft) return;

  if (col === 'draft') {
    colDraft.classList.add('visible');
    colOrig.classList.remove('visible');
    btnDraft?.classList.add('active');
    btnOrig?.classList.remove('active');
  } else {
    colOrig.classList.add('visible');
    colDraft.classList.remove('visible');
    btnOrig?.classList.add('active');
    btnDraft?.classList.remove('active');
  }
}

// ── 投稿ボタン ───────────────────────────────────────

// スマホ: twitter:// アプリ起動 → 未インストールなら Web fallback
function getQuoteTweetUrl(draft = currentDraft) {
  return draft?.original_tweet?.tweet_url || '';
}

function appendQuoteUrl(text, quoteUrl) {
  const cleanText = String(text || '').trimEnd();
  const cleanUrl = String(quoteUrl || '').trim();
  if (!cleanUrl || cleanText.includes(cleanUrl)) return cleanText;
  return cleanText ? `${cleanText}\n\n${cleanUrl}` : cleanUrl;
}

async function arrangePostingWindows(targetUrl) {
  const url = String(targetUrl || '').trim() || 'https://x.com/compose/post';
  try {
    const res = await apiFetch(`${API}/window/split-posting`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        target_url: url,
        preview_url: window.location.href,
      }),
    });
    const data = await res.json();
    if (!res.ok || !data.ok) throw new Error(data.error || '配置に失敗しました');
    return true;
  } catch (e) {
    showToast(`ウィンドウ配置に失敗したため通常表示します: ${e.message}`, true);
    window.open(url, '_blank');
    return false;
  }
}

async function openXCompose(text, quoteUrl = '') {
  const quote = String(quoteUrl || '').trim();
  await arrangePostingWindows(quote || 'https://x.com/compose/post');
}

async function onPostClick(asQuote = false) {
  if (!currentDraft) return;
  currentDraft.postAsQuote = !!asQuote;
  const quoteUrl = getQuoteTweetUrl();
  await arrangePostingWindows(asQuote && quoteUrl ? quoteUrl : 'https://x.com/compose/post');
  openThreadModal();
}

async function startAutoPost(asQuote = false, options = {}) {
  showToast('自動投稿機能は無効化しました。Xを開いて手動で投稿してください。', true);
  return;
}

async function pollAutoPost() {
  if (!autoPostJob?.job_id) return;
  try {
    const res = await apiFetch(`${API}/auto-post/status?id=${encodeURIComponent(autoPostJob.job_id)}`);
    const data = await res.json();
    if (!res.ok || !data.ok) throw new Error(data.error || '不明');
    autoPostJob = data;
    renderAutoPostProgress(data);
    maybeShowAgentPromptCopiedPopup(data);
    if (data.status === 'pending_confirm') {
      clearInterval(autoPostTimer);
      autoPostTimer = null;
      renderAutoPostConfirm(data);
      return;
    }
    if (data.status === 'completed') {
      clearInterval(autoPostTimer);
      autoPostTimer = null;
      setAutoPostButtonState(false);
      showToast('✓ 自動投稿が完了しました');
      await refreshDraftAfterAutoPost(autoPostDraftId || currentDraft?.draft_id);
      autoPostDraftId = null;
      return;
    }
    if (data.status === 'failed') {
      clearInterval(autoPostTimer);
      autoPostTimer = null;
      setAutoPostButtonState(false);
      autoPostDraftId = null;
      showToast(`自動投稿失敗: ${data.message || '不明'}`, true);
    }
  } catch (e) {
    clearInterval(autoPostTimer);
    autoPostTimer = null;
    setAutoPostButtonState(false);
    maybeShowAgentPromptCopiedPopup(autoPostJob);
    autoPostDraftId = null;
    showToast(`自動投稿確認失敗: ${e.message}`, true);
  }
}

function maybeShowAgentPromptCopiedPopup(job) {
  if (!job?.agent_prompt_copied || !job.job_id || shownAgentPromptNotices.has(job.job_id)) return;
  shownAgentPromptNotices.add(job.job_id);
  showAgentPromptCopiedPopup(job.agent_prompt_notice, job.agent_prompt_text);
}

function setAutoPostButtonState(isRunning, asQuote = false) {
  const normalBtn = document.getElementById('auto-post-btn');
  const quoteBtn = document.getElementById('auto-quote-post-btn');
  [normalBtn, quoteBtn].forEach(btn => {
    if (btn) btn.disabled = !!isRunning;
  });
  if (normalBtn) normalBtn.innerHTML = isRunning && !asQuote ? '<span>…</span> 自動投稿中' : '<span>▶</span> 自動投稿';
  if (quoteBtn) quoteBtn.innerHTML = isRunning && asQuote ? '<span>…</span> 引用自動投稿中' : '<span>▶</span> 引用自動投稿';
}

function renderAutoPostProgress(job) {
  const slot = document.getElementById('auto-post-progress-slot');
  if (!slot || !job) return;
  const progress = Math.max(0, Math.min(100, Number(job.progress || 0)));
  const logs = Array.isArray(job.logs) ? job.logs.slice(-10) : [];
  const logBlock = logs.length
    ? `<div class="auto-post-log">
        ${logs.map(log => `
          <div class="image-generation-log-row ${escAttr(log.level || 'info')}">
            <span class="image-generation-log-time">${esc(log.at || '')}</span>
            <span class="image-generation-log-message">${esc(log.message || '')}</span>
          </div>`).join('')}
      </div>`
    : '';
  slot.innerHTML = `
    <div class="auto-post-progress ${job.status === 'failed' ? 'failed' : ''}">
      <div class="image-generation-progress-top">
        <span>${esc(job.message || '自動投稿中です')}</span>
        <span>${progress}%</span>
      </div>
      <div class="image-generation-bar"><div style="width:${progress}%"></div></div>
      ${logBlock}
    </div>`;
}

async function sendAutoPostAnswer(answer) {
  const job = autoPostJob;
  if (!job?.job_id) return;
  const slot = document.getElementById('auto-post-progress-slot');
  if (slot) slot.innerHTML = '<div class="auto-post-progress"><span>処理中...</span></div>';
  try {
    const res = await apiFetch(`${API}/auto-post/confirm`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ job_id: job.job_id, answer }),
    });
    const data = await res.json();
    if (!data.ok) throw new Error(data.error || '不明');
    if (answer === 'confirm') {
      autoPostTimer = setInterval(pollAutoPost, 2500);
    } else {
      setAutoPostButtonState(false);
      autoPostDraftId = null;
      showToast('投稿をキャンセルしました');
    }
  } catch (e) {
    setAutoPostButtonState(false);
    autoPostDraftId = null;
    showToast(`エラー: ${e.message}`, true);
  }
}

function renderAutoPostConfirm(job) {
  const slot = document.getElementById('auto-post-progress-slot');
  if (!slot) return;
  const imgPath = job.confirm_screenshot || '';
  const imgTag = imgPath
    ? `<img src="/local-image?path=${encodeURIComponent(imgPath)}" style="width:100%;border-radius:8px;margin:8px 0;" alt="投稿プレビュー">`
    : '';
  slot.innerHTML = `
    <div class="auto-post-progress">
      <div class="image-generation-progress-top">
        <span>${materialIcon('assignment', { filled: true })}投稿内容を確認してください</span>
      </div>
      ${imgTag}
      <div style="display:flex;gap:8px;margin-top:8px;">
        <button class="cancel-btn" onclick="sendAutoPostAnswer('cancel')">キャンセル</button>
        <button class="save-btn" style="flex:1" onclick="sendAutoPostAnswer('confirm')">${materialIcon('check_circle', { filled: true })}この内容で投稿する</button>
      </div>
    </div>`;
}

function showAgentPromptCopiedPopup(message, promptText = '') {
  const existing = document.getElementById('agent-prompt-popup');
  if (existing) existing.remove();
  const overlay = document.createElement('div');
  overlay.id = 'agent-prompt-popup';
  overlay.className = 'modal-overlay agent-prompt-overlay';
  overlay.innerHTML = `
    <div class="modal agent-prompt-modal" role="dialog" aria-modal="true" aria-labelledby="agent-prompt-title" onclick="event.stopPropagation()">
      <h3 id="agent-prompt-title">改善指示をコピーしました</h3>
      <p class="modal-msg">${esc(message || 'AIエージェントへの改善指示をクリップボードに保存しました。AIエージェントに指示をお願いします。')}</p>
      <textarea class="agent-prompt-text" readonly>${esc(promptText || '')}</textarea>
      <div class="modal-actions">
        <button class="save-btn" onclick="document.getElementById('agent-prompt-popup')?.remove()">OK</button>
      </div>
    </div>`;
  overlay.onclick = () => overlay.remove();
  document.body.appendChild(overlay);
}

function showChoiceModal({ title, message, primary, secondary = '', cancel = 'キャンセル' }) {
  return new Promise(resolve => {
    const existing = document.getElementById('choice-modal');
    if (existing) existing.remove();
    const overlay = document.createElement('div');
    overlay.id = 'choice-modal';
    overlay.className = 'modal-overlay';
    overlay.innerHTML = `
      <div class="modal choice-modal" role="dialog" aria-modal="true" aria-labelledby="choice-modal-title" onclick="event.stopPropagation()">
        <h3 id="choice-modal-title">${esc(title || '確認')}</h3>
        <p class="modal-msg">${esc(message || '')}</p>
        <div class="modal-actions">
          <button class="cancel-btn" data-choice="cancel">${esc(cancel)}</button>
          ${secondary ? `<button class="btn-ghost" data-choice="secondary">${esc(secondary)}</button>` : ''}
          <button class="save-btn" data-choice="primary">${esc(primary || 'OK')}</button>
        </div>
      </div>`;
    const close = choice => {
      overlay.remove();
      resolve(choice);
    };
    overlay.onclick = () => close('cancel');
    overlay.querySelectorAll('[data-choice]').forEach(button => {
      button.addEventListener('click', () => close(button.dataset.choice || 'cancel'));
    });
    document.body.appendChild(overlay);
  });
}

async function refreshDraftAfterAutoPost(draftId) {
  if (!draftId) return;
  const reloadRes = await apiFetch(`${API}/draft?id=${encodeURIComponent(draftId)}`);
  const draft = await reloadRes.json();
  const item = allDrafts.find(d => d.draft_id === draft.draft_id);
  if (item) {
    item.status = draft.status;
    item.published_at = draft.published_at;
    item.parts = draft.parts;
    item.memo = draft.memo;
  }
  if (currentDraft?.draft_id === draft.draft_id) {
    currentDraft = draft;
    renderDraftList();
    renderPreview(currentDraft);
  } else {
    renderDraftList();
  }
}

// ── ツリー投稿ガイド ────────────────────────────────
let threadStep = 0;

function openThreadModal() {
  threadStep = 0;
  renderThreadStep();
  document.getElementById('thread-modal').style.display = 'flex';
}

function closeThreadModal() {
  document.getElementById('thread-modal').style.display = 'none';
}

function handleThreadModalBg(e) {
  if (e.target.id === 'thread-modal') closeThreadModal();
}

function renderThreadStep() {
  const parts  = currentDraft.parts;
  const part   = parts[threadStep];
  const isFirst = threadStep === 0;
  const isLast  = threadStep === parts.length - 1;

  document.getElementById('thread-step-label').textContent =
    `パート ${threadStep + 1} / ${parts.length}`;
  document.getElementById('thread-part-content').textContent = part.content;
  document.getElementById('thread-instruction').textContent = isFirst
    ? (currentDraft.postAsQuote
      ? '① 開いた元投稿で引用リツイートを押してから、この内容をコピーして貼り付けてください'
      : '① X の投稿画面を開いて、この内容をコピーして貼り付けてください')
    : `② 前のツイートへの返信として投稿してください（パート ${threadStep + 1}）`;
  const copyImageBtn = document.getElementById('thread-copy-image-btn');
  if (copyImageBtn) copyImageBtn.style.display = part.image_url ? '' : 'none';
  const openBtn = document.getElementById('thread-open-btn');
  openBtn.style.display = isFirst ? '' : 'none';
  openBtn.innerHTML = currentDraft.postAsQuote
    ? '<span class="x-mark" aria-hidden="true">𝕏</span>元投稿を開く'
    : '<span class="x-mark" aria-hidden="true">𝕏</span>X 投稿画面を開く';
  document.getElementById('thread-prev-btn').disabled = isFirst;
  document.getElementById('thread-next-btn').style.display = isLast ? 'none' : '';
  document.getElementById('thread-done-btn').style.display = isLast ? '' : 'none';
}

async function copyThreadPart() {
  const part = currentDraft.parts[threadStep];
  const text = part.content;
  try {
    await navigator.clipboard.writeText(text);
    showToast('コピーしました');
  } catch {
    showToast('コピーに失敗しました', true);
  }
}

async function copyThreadImage() {
  const part = currentDraft?.parts?.[threadStep];
  if (!part?.image_url) {
    showToast('このパートには画像がありません', true);
    return;
  }
  await copyGeneratedImageFromUrl(part.image_url);
}

async function openXForThread() {
  await openXCompose('', currentDraft.postAsQuote ? getQuoteTweetUrl() : '');
}

function prevThreadPart() {
  if (threadStep > 0) { threadStep--; renderThreadStep(); }
}

function nextThreadPart() {
  if (threadStep < currentDraft.parts.length - 1) { threadStep++; renderThreadStep(); }
}

async function doneThreadPost() {
  const draft = currentDraft;
  if (!draft?.draft_id) {
    showToast('対象の下書きが見つかりません', true);
    return;
  }
  const doneBtn = document.getElementById('thread-done-btn');
  const marked = await setStatus(draft.draft_id, 'published', { button: doneBtn });
  if (!marked) return;
  closeThreadModal();
  notifyDiscord(draft.parts?.[0]?.content || '', { silent: true });
}

async function notifyDiscord(mainContent, options = {}) {
  const silent = !!options.silent;
  const btn = silent ? null : document.querySelector('.post-btn');
  if (btn) { btn.disabled = true; btn.textContent = '送信中…'; }

  try {
    const res  = await apiFetch(`${API}/notify`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        draft_id:     currentDraft.draft_id,
        main_content: mainContent,
        x_username:   currentDraft.x_username,
      }),
    });
    const data = await res.json();
    if (!silent) {
      showToast(data.ok ? '✓ Discord に通知しました' : `Discord 通知失敗: ${data.error || '不明'}`, !data.ok);
    } else if (!data.ok) {
      console.warn('Discord notification failed:', data.error || 'unknown error');
    }
  } catch (e) {
    if (!silent) showToast(`通知エラー: ${e.message}`, true);
    else console.warn('Discord notification error:', e);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = '<span class="x-mark" aria-hidden="true">𝕏</span>投稿する';
    }
  }
}

async function saveToObsidian() {
  if (!currentDraft?.draft_id) return;
  const draftId = String(currentDraft.draft_id);
  if (savedObsidianDraftIds.has(draftId)) {
    openSavedObsidian();
    return;
  }
  const btn = document.getElementById('obsidian-save-btn');
  const status = document.getElementById('obsidian-save-status');
  let saved = false;
  let canOpen = false;
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = '<span class="inline-spinner" aria-hidden="true"></span><span>保存中...</span>';
  }
  if (status) {
    status.textContent = '保存しています…';
    status.classList.remove('is-error');
  }

  try {
    const res = await apiFetch(`${API}/obsidian/save`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ draft_id: currentDraft.draft_id }),
    });
    const data = await res.json();
    if (!data.ok) throw new Error(data.error || '保存に失敗しました');
    saved = true;
    const saveData = { saved: true, ...data };
    lastObsidianSave = saveData;
    savedObsidianDraftIds.add(draftId);
    obsidianSaveByDraftId.set(draftId, saveData);
    const listItem = allDrafts.find(item => String(item.draft_id) === draftId);
    if (listItem) listItem.obsidian_save = saveData;
    if (currentDraft && String(currentDraft.draft_id) === draftId) currentDraft.obsidian_save = saveData;
    const savedPath = data.relative_path || data.path || '保存先不明';
    showToast(`✓ Obsidianに保存しました: ${savedPath}`);
    if (status) {
      status.textContent = '';
      status.title = data.path || savedPath;
      status.classList.remove('is-error');
    }
    canOpen = !!data.obsidian_url;
    renderDraftList();
  } catch (e) {
    showToast(`Obsidian保存エラー: ${e.message}`, true);
    if (status) {
      status.textContent = `保存エラー: ${e.message}`;
      status.classList.add('is-error');
    }
  } finally {
    if (btn) {
      btn.disabled = saved && !canOpen;
      btn.classList.toggle('is-saved', saved);
      if (saved && canOpen) {
        btn.innerHTML = `${materialIcon('open_in_new')}Obsidianで開く`;
        btn.onclick = openSavedObsidian;
      } else if (saved) {
        btn.innerHTML = `${materialIcon('check_circle', { filled: true })}保存済み`;
      } else {
        btn.innerHTML = `${materialIcon('save')}Obsidianへ保存`;
      }
    }
  }
}

function openSavedObsidian() {
  const draftId = currentDraft?.draft_id ? String(currentDraft.draft_id) : '';
  const saved = (draftId && obsidianSaveByDraftId.get(draftId)) || lastObsidianSave;
  if (!saved?.obsidian_url) {
    showToast('先にObsidianへ保存してください', true);
    return;
  }
  window.open(saved.obsidian_url, '_blank');
}

// ── ステータス変更（投稿済み ⇔ 未投稿） ─────────────

async function setStatus(draftId, status, options = {}) {
  const button = options.button || (status === 'published'
    ? document.getElementById('mark-posted-btn')
    : document.querySelector('.revert-draft-btn'));
  const previousHtml = button?.innerHTML || '';
  if (button) {
    button.disabled = true;
    button.innerHTML = '<span class="inline-spinner" aria-hidden="true"></span><span>更新中...</span>';
  }
  try {
    const res  = await apiFetch(`${API}/draft/status`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ draft_id: draftId, status }),
    });
    const data = await readApiJson(res, 'ステータス更新API');
    if (!res.ok || !data.ok) throw new Error(data.error || `HTTP ${res.status}`);

    const reloadRes = await apiFetch(`${API}/draft?id=${draftId}`);
    const freshDraft = await readApiJson(reloadRes, '下書き再取得API');
    if (!reloadRes.ok || freshDraft.error) throw new Error(freshDraft.error || `HTTP ${reloadRes.status}`);
    if (freshDraft.status !== status) {
      throw new Error(`更新後の状態が ${freshDraft.status || '不明'} のままです`);
    }

    currentDraft = freshDraft;
    const listItem = allDrafts.find(d => d.draft_id === draftId);
    if (listItem) {
      listItem.status = freshDraft.status;
      listItem.published_at = freshDraft.published_at || null;
      listItem.has_image = !!freshDraft.has_image;
      listItem.part_count = freshDraft.parts?.length || listItem.part_count || 1;
      listItem.preview_content = freshDraft.parts?.[0]?.content || listItem.preview_content || '';
      listItem.parts = freshDraft.parts;
    }

    renderDraftList();
    renderPreview(currentDraft);

    showToast(status === 'published' ? '✓ 投稿済みにしました' : '↩ 未投稿に戻しました');
    return true;
  } catch (e) {
    if (button) {
      button.disabled = false;
      button.innerHTML = previousHtml;
    }
    showToast(`ステータス更新エラー: ${e.message}`, true);
    return false;
  }
}

// ── 削除 ─────────────────────────────────────────────

function confirmDelete(event, draftId) {
  event.stopPropagation();
  pendingDeleteId = draftId;
  document.getElementById('delete-modal').style.display = 'flex';
  document.getElementById('modal-confirm-btn').onclick = () => deleteDraft(draftId);
}

function closeDeleteModal() {
  pendingDeleteId = null;
  document.getElementById('delete-modal').style.display = 'none';
}

async function deleteDraft(draftId) {
  closeDeleteModal();
  try {
    const res  = await apiFetch(`${API}/draft?id=${draftId}`, { method: 'DELETE' });
    const data = await res.json();
    if (!data.ok) throw new Error(data.error || '不明');

    // ローカルキャッシュから削除
    allDrafts = allDrafts.filter(d => d.draft_id !== draftId);

    // プレビューリセット
    if (currentDraft?.draft_id === draftId) {
      currentDraft = null;
      const next = allDrafts[0];
      if (next) {
        selectDraft(next.draft_id);
      } else {
        document.getElementById('preview-content').innerHTML = `
          <div class="empty-state">
            <div class="x-logo-large">𝕏</div>
            <p>下書きがありません</p>
          </div>`;
      }
    }

    renderDraftList();
    showToast('🗑 削除しました');
  } catch (e) {
    showToast(`削除エラー: ${e.message}`, true);
  }
}

// ── インライン編集 ───────────────────────────────────

function startEdit(partId) {
  // currentDraft から本文を取得（onclick 属性に本文を埋め込むと特殊文字で壊れるため）
  const part = currentDraft?.parts.find(p => p.part_id === partId);
  if (!part) return;
  const originalContent = part.content;

  const textEl = document.getElementById(`text-${partId}`);
  if (!textEl) return;

  const parent = textEl.parentElement;
  textEl.style.display = 'none';

  const editArea = document.createElement('div');
  editArea.className = 'tweet-edit-area';
  editArea.id        = `edit-${partId}`;

  const textarea = document.createElement('textarea');
  textarea.className = 'tweet-textarea';
  textarea.value     = originalContent;

  const actions = document.createElement('div');
  actions.className  = 'edit-actions';

  const saveBtn = document.createElement('button');
  saveBtn.type      = 'button';
  saveBtn.className = 'save-btn';
  saveBtn.textContent = '保存';
  saveBtn.onclick    = () => {
    // クリックした瞬間の値を取得
    saveEdit(partId, textarea.value, originalContent);
  };

  const cancelBtn = document.createElement('button');
  cancelBtn.type      = 'button';
  cancelBtn.className = 'cancel-btn';
  cancelBtn.textContent = 'キャンセル';
  cancelBtn.onclick    = () => cancelEdit(partId, originalContent);

  actions.append(saveBtn, cancelBtn);
  editArea.append(textarea, actions);
  parent.insertBefore(editArea, textEl);

  textarea.focus();
  textarea.setSelectionRange(textarea.value.length, textarea.value.length);
}

async function saveEdit(partId, newContent, originalContent) {
  if (newContent === originalContent) {
    cancelEdit(partId, originalContent);
    return;
  }
  try {
    const res  = await apiFetch(`${API}/draft/part`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ part_id: partId, content: newContent }),
    });
    const data = await res.json();
    if (!data.ok) throw new Error(data.error || '不明');

    // ローカルキャッシュ更新
    if (currentDraft && currentDraft.parts) {
      const part = currentDraft.parts.find(p => p.part_id === partId);
      if (part) part.content = newContent;

      // 一覧キャッシュ (allDrafts) も更新して、サイドバーの表示を即座に変える
      const d = allDrafts.find(item => item.draft_id === currentDraft.draft_id);
      if (d) {
        // プレビュー文字列を更新
        d.preview_content = newContent;
        // もしパーツデータがあればそれも更新
        if (d.parts) {
          const dp = d.parts.find(p => p.part_id === partId);
          if (dp) dp.content = newContent;
        }
      }
    }

    cancelEdit(partId, newContent);
    renderDraftList();
    showToast('✓ 保存しました');
  } catch (e) {
    showToast(`保存エラー: ${e.message}`, true);
  }
}

function cancelEdit(partId, content) {
  const editArea = document.getElementById(`edit-${partId}`);
  const textEl   = document.getElementById(`text-${partId}`);
  if (editArea) editArea.remove();
  if (textEl) {
    textEl.innerHTML = formatText(content);
    textEl.style.display = '';
  }
}

// ── モバイルタブ切り替え ─────────────────────────────

function switchTab(tab) {
  const sidebar  = document.getElementById('sidebar');
  const preview  = document.getElementById('preview-area');
  const btnList  = document.getElementById('tab-list-btn');
  const btnPrev  = document.getElementById('tab-preview-btn');

  if (tab === 'list') {
    sidebar.classList.add('tab-active');
    preview.classList.remove('tab-active');
    btnList.classList.add('active');
    btnPrev.classList.remove('active');
  } else {
    sidebar.classList.remove('tab-active');
    preview.classList.add('tab-active');
    btnList.classList.remove('active');
    btnPrev.classList.add('active');
  }
}

// モバイル初期表示はリスト側を出す
function initMobileTab() {
  if (window.innerWidth <= 640) {
    document.getElementById('sidebar').classList.add('tab-active');
  }
}

function syncMobileTabForView() {
  if (window.innerWidth > 640) return;
  const sidebar = document.getElementById('sidebar');
  const preview = document.getElementById('preview-area');
  if (activeView === 'drafts') {
    if (!sidebar?.classList.contains('tab-active') && !preview?.classList.contains('tab-active')) {
      switchTab('list');
    }
    return;
  }
  switchTab('preview');
}

// ── ユーティリティ ───────────────────────────────────

function showToast(msg, isError = false) {
  const existing = document.getElementById('toast');
  if (existing) existing.remove();

  const toast       = document.createElement('div');
  toast.id          = 'toast';
  toast.className   = `toast${isError ? ' toast-error' : ''}`;
  const text = String(msg || '');
  const isSuccess = !isError && text.trim().startsWith('✓');
  const isUndo = !isError && text.trim().startsWith('↩');
  const cleanText = text.replace(/^\s*[✓↩]\s*/, '');
  const iconName = isError ? 'error' : isUndo ? 'undo' : isSuccess ? 'check_circle' : '';
  toast.innerHTML = iconName
    ? `${materialIcon(iconName, { filled: isError || isSuccess })}<span>${esc(cleanText)}</span>`
    : esc(text);
  document.body.appendChild(toast);

  requestAnimationFrame(() => toast.classList.add('toast-show'));
  setTimeout(() => {
    toast.classList.remove('toast-show');
    setTimeout(() => toast.remove(), 300);
  }, 3500);
}

function formatText(text) {
  return esc(text)
    .replace(/(https?:\/\/\S+)/g, '<a href="$1" target="_blank">$1</a>')
    .replace(/#(\S+)/g,  '<span style="color:var(--accent)">#$1</span>')
    .replace(/@(\w+)/g,  '<span style="color:var(--accent)">@$1</span>');
}

function esc(str) {
  return String(str ?? '')
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}

function escAttr(str) {
  return String(str ?? '')
    .replace(/&/g,'&amp;')
    .replace(/"/g,'&quot;')
    .replace(/</g,'&lt;')
    .replace(/>/g,'&gt;');
}

function normalizeXProfileImageUrl(url) {
  return String(url || '').replace('_normal.', '_400x400.');
}

function getCharacterSetting() {
  return currentDraft?.character_setting || {};
}

function getCharacterReferenceUrl() {
  return getCharacterSetting().reference_url ||
    currentDraft?.image_prompts?.[0]?.character_reference_url ||
    currentDraft?.character_reference?.url ||
    normalizeXProfileImageUrl(currentDraft?.profile_image_url);
}

function getCharacterTraits() {
  return getCharacterSetting().traits ||
    '半目、眠そうな表情、舌を少し出す、頬杖、黒い短髪、黒いスーツ、白シャツ、赤ネクタイ、気だるい表情';
}

function getCharacterNegative() {
  return getCharacterSetting().negative ||
    '明るい丸目の少年、パーカー、元気な笑顔、指差しポーズ';
}

function getCharacterPlacement() {
  return getCharacterSetting().placement ||
    '右下20〜25%。図解が主役、キャラクターは補助。';
}

// ── 起動 ─────────────────────────────────────────────

initMobileTab();
init();
window.addEventListener('resize', syncMobileTabForView);
