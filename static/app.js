const state = {
  status: null,
  currentAnalysis: null,
  currentApprovalId: null,
  sessionId: localStorage.getItem("kis_session") || null,
  // Dashboard token kept only for the browser session (not persisted to disk).
  dashboardToken: sessionStorage.getItem("kis_dash_token") || "",
  chartPeriod: "3M",
  chartShowBB: false,
  // Chart history cache: the fullest bar set loaded for the current symbol, and how
  // many days we've already requested (so switching periods only fetches when needed).
  chartBars: [],
  chartReqDays: 0,
  chartKey: "",
};

const PERIOD_BARS = { "1M": 22, "3M": 66, "6M": 130, "1Y": 252, "5Y": 1260, "10Y": 2520, "MAX": Infinity };
// Calendar days of history to request from the backend per period (generous so
// enough trading bars come back; the backend caps and paginates until data runs out).
const PERIOD_DAYS = { "1M": 40, "3M": 110, "6M": 200, "1Y": 400, "5Y": 1950, "10Y": 3900, "MAX": 8000 };
const COMPARE_COLORS = ["#1f6feb", "#16845b", "#c13b3a", "#9a6a00", "#7c4dff", "#0aa", "#e8890c", "#d6336c"];

const $ = (selector) => document.querySelector(selector);
const fmt = new Intl.NumberFormat("ko-KR");

document.addEventListener("DOMContentLoaded", async () => {
  bindTabs();
  bindForms();
  await restoreConversation();
  await loadStatus();
  await loadConfig();
  await loadMe();
  await loadLogs();
  if (window.lucide) window.lucide.createIcons();
});

// Google login state. When OAuth is enabled, the token field is replaced by a
// "Sign in with Google" button (or the logged-in email + logout).
async function loadMe() {
  const box = $("#authBox");
  const authLabel = $("#authLabel");
  const tokenLabel = $("#dashTokenLabel");
  const tokenInput = $("#dashTokenInput");
  let me;
  try {
    me = await api("/api/me");
  } catch (error) {
    return;
  }
  if (!me.oauth_enabled) {
    // No Google OAuth configured — keep the legacy token field, hide the account row.
    if (authLabel) authLabel.classList.add("hidden");
    if (box) box.classList.add("hidden");
    return;
  }
  // OAuth on: login is the ONLY unlock. Drop any stale legacy token so the browser
  // stops sending X-Dashboard-Token (otherwise old sessions stay privileged).
  state.dashboardToken = "";
  sessionStorage.removeItem("kis_dash_token");

  // Hide the token field, show login/logout.
  if (tokenLabel) tokenLabel.classList.add("hidden");
  if (tokenInput) tokenInput.classList.add("hidden");
  if (authLabel) authLabel.classList.remove("hidden");
  if (box) {
    box.classList.remove("hidden");
    if (me.email) {
      box.innerHTML =
        `<div class="auth-user"><span>✅ 로그인됨</span><b>${me.email}</b>` +
        `<a class="auth-link" href="/auth/logout">로그아웃</a></div>`;
    } else {
      box.innerHTML =
        `<a class="auth-btn" href="/auth/login"><i data-lucide="log-in"></i> Google로 로그인</a>` +
        `<p class="neutral" style="margin-top:8px;font-size:12px">주문·잔고·환경전환은 로그인 후 사용할 수 있습니다.</p>`;
    }
  }
  // Lock the privileged toggles until an allowlisted user is logged in.
  setPrivilegedControls(me.authorized);
  if (window.lucide) window.lucide.createIcons();
}

// Enable/disable privileged controls based on whether the request is authorized.
function setPrivilegedControls(authorized) {
  ["#liveOrders", "#autoTrade", "#autoPilot", "#autoPilotLlm", "#envSelect"].forEach((sel) => {
    const el = $(sel);
    if (el) el.disabled = !authorized;
  });
}

function seedChatThread() {
  $("#chatThread").innerHTML =
    `<div class="chat-empty">무엇을 도와드릴까요? 종목 분석·비교, 잔고, 주문 초안을 요청해 보세요.</div>`;
}

// Restore the persisted conversation (server-side) so a page refresh keeps context.
async function restoreConversation() {
  if (!state.sessionId) {
    seedChatThread();
    return;
  }
  try {
    const conv = await api(`/api/conversations/${state.sessionId}`);
    $("#chatThread").innerHTML = "";
    (conv.messages || []).forEach((m) => appendBubble(m.role, m.content));
    if (!conv.messages || !conv.messages.length) seedChatThread();
  } catch (error) {
    // Unknown/expired session — start fresh.
    state.sessionId = null;
    localStorage.removeItem("kis_session");
    seedChatThread();
  }
}

function newConversation() {
  state.sessionId = null;
  localStorage.removeItem("kis_session");
  seedChatThread();
  $("#chatInput").focus();
}

function bindTabs() {
  document.querySelectorAll(".tab").forEach((button) => {
    button.addEventListener("click", () => showTab(button.dataset.tab));
  });
}

function bindForms() {
  $("#analyzeForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    await analyze($("#symbolInput").value);
  });

  $("#screenForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const symbols = $("#watchlistInput").value.split(/[,\s]+/).filter(Boolean);
    await screen(symbols);
  });

  $("#orderForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    await previewOrder();
  });

  $("#executeButton").addEventListener("click", executeOrder);

  $("#chatForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const input = $("#chatInput");
    const message = input.value.trim();
    if (!message) return;
    input.value = "";
    await chat(message);
  });

  $("#refreshLogs").addEventListener("click", loadLogs);
  $("#refreshBalance").addEventListener("click", () => loadBalance(true));
  $("#balanceMarket").addEventListener("change", () => loadBalance(true));
  $("#newChat").addEventListener("click", newConversation);
  $("#modelSelect").addEventListener("change", saveConfig);
  $("#thinkingToggle").addEventListener("change", saveConfig);
  $("#envSelect").addEventListener("change", saveEnv);
  $("#dryRunDefault").addEventListener("change", saveDryRunDefault);
  $("#dashTokenInput").addEventListener("input", (event) => {
    state.dashboardToken = event.target.value.trim();
    if (state.dashboardToken) sessionStorage.setItem("kis_dash_token", state.dashboardToken);
    else sessionStorage.removeItem("kis_dash_token");
  });
  $("#maxOrderKrw").addEventListener("change", saveLimits);
  $("#maxOrderUsd").addEventListener("change", saveLimits);
  $("#liveOrders").addEventListener("change", saveLiveOrders);
  $("#autoTrade").addEventListener("change", () => saveAuto({ auto_trade: $("#autoTrade").checked }));
  $("#autoPilot").addEventListener("change", () => saveAuto({ auto_pilot: $("#autoPilot").checked }));
  $("#autoPilotLlm").addEventListener("change", () => saveAuto({ auto_pilot_llm: $("#autoPilotLlm").checked }));
  $("#autoInterval").addEventListener("change", () => {
    const v = Number($("#autoInterval").value);
    if (v >= 15) saveAuto({ auto_pilot_interval: Math.round(v) });
  });
}

async function saveAuto(body) {
  try {
    const cfg = await api("/api/config", body);
    applyConfig(cfg);
    setSettingsHint(cfg.auto_pilot_running ? "⚠️ 자율매매 작동 중" : "자동매매 설정 적용됨");
  } catch (error) {
    setSettingsHint(`설정 실패: ${error.message}`, true);
    await loadConfig();
  }
}

async function saveLimits() {
  const body = {};
  const krw = Number($("#maxOrderKrw").value);
  const usd = Number($("#maxOrderUsd").value);
  if (krw > 0) body.max_order_krw = Math.round(krw);
  if (usd > 0) body.max_order_usd = usd;
  if (!Object.keys(body).length) return;
  try {
    applyConfig(await api("/api/config", body));
    setSettingsHint("주문 한도 적용됨");
  } catch (error) {
    setSettingsHint(`한도 변경 실패: ${error.message}`, true);
    await loadConfig();
  }
}

async function saveLiveOrders() {
  try {
    applyConfig(await api("/api/config", { allow_live_orders: $("#liveOrders").checked }));
    setSettingsHint($("#liveOrders").checked ? "⚠️ 실주문 허용됨" : "실주문 잠금");
  } catch (error) {
    setSettingsHint(`실주문 설정 실패: ${error.message}`, true);
    await loadConfig();
  }
}

async function saveEnv() {
  const env = $("#envSelect").value;
  setSettingsHint("환경 전환 중…");
  try {
    const cfg = await api("/api/config", { environment: env });
    applyConfig(cfg);
    setSettingsHint(`현재 환경: ${cfg.environment}`);
  } catch (error) {
    setSettingsHint(`전환 실패: ${error.message}`, true);
    await loadConfig(); // revert select to the actual server state
  }
}

async function saveDryRunDefault() {
  try {
    applyConfig(await api("/api/config", { dry_run_default: $("#dryRunDefault").checked }));
  } catch (error) {
    setSettingsHint(`설정 실패: ${error.message}`, true);
  }
}

function setSettingsHint(message, isError) {
  const hint = $("#settingsHint");
  if (!hint) return;
  hint.textContent = message || "";
  hint.classList.toggle("danger-text", !!isError);
}

async function loadConfig() {
  try {
    applyConfig(await api("/api/config"));
  } catch (error) {
    /* keep defaults if config can't load */
  }
}

async function saveConfig() {
  try {
    const cfg = await api("/api/config", {
      model: $("#modelSelect").value,
      thinking: $("#thinkingToggle").checked,
    });
    applyConfig(cfg);
  } catch (error) {
    appendBubble("assistant", `설정 변경 실패: ${error.message}`, null, true);
  }
}

function applyConfig(cfg) {
  const select = $("#modelSelect");
  select.innerHTML = (cfg.available_models || [])
    .map((m) => `<option value="${m.id}">${escapeHtml(m.label)}</option>`)
    .join("");
  select.value = cfg.model;
  const toggle = $("#thinkingToggle");
  toggle.checked = !!cfg.thinking;
  select.disabled = !cfg.llm_ready;
  toggle.disabled = !cfg.llm_ready || !cfg.thinking_supported;
  const badge = $("#llmBadge");
  if (badge) {
    if (!cfg.llm_ready) {
      badge.textContent = "키워드 모드";
    } else {
      const short = (cfg.model || "").replace("claude-", "").replace(/-/g, " ");
      badge.textContent = `${short}${cfg.thinking && cfg.thinking_supported ? " · 🧠" : ""}`;
    }
  }

  // Environment selector — disable options that can't be switched to (no creds / no token).
  const envSel = $("#envSelect");
  if (envSel && cfg.environment) {
    const available = cfg.available_environments || ["mock"];
    Array.from(envSel.options).forEach((opt) => {
      opt.disabled = !available.includes(opt.value) && opt.value !== cfg.environment;
    });
    envSel.value = cfg.environment;
    const eb = $("#envBadge");
    if (eb) {
      eb.textContent = cfg.environment;
      eb.className = `badge ${cfg.environment === "prod" ? "danger" : cfg.environment === "paper" ? "caution" : ""}`;
    }
  }

  // Default dry-run mirrors into the order ticket checkbox.
  if (cfg.dry_run_default !== undefined) {
    const dd = $("#dryRunDefault");
    if (dd) dd.checked = !!cfg.dry_run_default;
    const orderDry = $("#dryRun");
    if (orderDry) orderDry.checked = !!cfg.dry_run_default;
  }

  // Per-order limits.
  const mk = $("#maxOrderKrw");
  if (mk && cfg.max_order_krw !== undefined) mk.value = cfg.max_order_krw;
  const mu = $("#maxOrderUsd");
  if (mu && cfg.max_order_usd !== undefined) mu.value = cfg.max_order_usd;
  const limitBadge = $("#limitBadge");
  if (limitBadge && cfg.max_order_krw !== undefined) {
    limitBadge.textContent = `1회 ${fmt.format(cfg.max_order_krw)}원`;
  }

  // Live-order lock — only togglable when a dashboard token is configured.
  const live = $("#liveOrders");
  if (live) {
    live.checked = !!cfg.live_orders_enabled;
    live.disabled = !cfg.auth_required;
  }
  const liveBadge = $("#liveBadge");
  if (liveBadge) {
    liveBadge.textContent = cfg.live_orders_enabled ? "live on" : "live locked";
    liveBadge.className = `badge ${cfg.live_orders_enabled ? "caution" : "muted"}`;
  }

  // Auto-trade controls.
  const autoTrade = $("#autoTrade");
  if (autoTrade) autoTrade.checked = !!cfg.auto_trade;
  const autoPilot = $("#autoPilot");
  if (autoPilot) autoPilot.checked = !!cfg.auto_pilot;
  const autoLlm = $("#autoPilotLlm");
  if (autoLlm) {
    autoLlm.checked = !!cfg.auto_pilot_llm;
    autoLlm.disabled = !cfg.auth_required;
  }
  const autoInt = $("#autoInterval");
  if (autoInt && cfg.auto_pilot_interval !== undefined) autoInt.value = cfg.auto_pilot_interval;

  // Dashboard token field: reflect stored value + whether the server requires one.
  const tokenInput = $("#dashTokenInput");
  if (tokenInput) {
    tokenInput.value = state.dashboardToken;
    tokenInput.placeholder = cfg.auth_required
      ? "paper/prod·실주문·전환에 필요"
      : "토큰 미설정 (현재 불필요)";
  }
}

async function loadBalance(focus) {
  const market = ($("#balanceMarket") && $("#balanceMarket").value) || "KR";
  try {
    const data = await api(`/api/balance?market=${market}`);
    renderBalance(data);
    if (focus) showTab("balance");
  } catch (error) {
    renderError("#balanceContent", error.message);
  }
}

async function loadStatus() {
  try {
    state.status = await api("/api/status");
  } catch (error) {
    $("#envBadge").textContent = "offline";
    $("#envBadge").className = "badge danger";
    appendBubble("assistant", `서버 상태를 불러오지 못했습니다: ${error.message}`, null, true);
    return;
  }
  $("#envBadge").textContent = state.status.environment;
  $("#envBadge").className = `badge ${state.status.environment === "prod" ? "danger" : ""}`;
  $("#liveBadge").textContent = state.status.live_orders_enabled ? "live on" : "live locked";
  $("#liveBadge").className = `badge ${state.status.live_orders_enabled ? "caution" : "muted"}`;
  $("#limitBadge").textContent = `1회 ${fmt.format(state.status.max_order_krw)}원`;
  const llm = $("#llmBadge");
  if (llm) llm.textContent = state.status.llm_ready ? "AI 대화" : "키워드 모드";
  $("#watchlistInput").value = state.status.default_watchlist.join(",");
}

async function analyze(symbol) {
  withLoading("#analyzeForm", true);
  try {
    const data = await api("/api/analyze", { symbol });
    state.currentAnalysis = data;
    renderReport(data);
    hydrateOrderTicket(data);
    showTab("report");
    await loadLogs();
  } catch (error) {
    renderError("#reportContent", error.message);
    $("#reportContent").classList.remove("hidden");
    $("#reportEmpty").classList.add("hidden");
  } finally {
    withLoading("#analyzeForm", false);
  }
}

async function screen(symbols) {
  withLoading("#screenForm", true);
  try {
    const data = await api("/api/screen", { symbols });
    renderScreen(data);
    showTab("screen");
    await loadLogs();
  } catch (error) {
    renderError("#screenContent", error.message);
  } finally {
    withLoading("#screenForm", false);
  }
}

async function previewOrder() {
  const body = {
    symbol: $("#orderSymbol").value,
    side: $("#orderSide").value,
    quantity: Number($("#orderQty").value),
    limit_price: Number($("#orderPrice").value),
    dry_run: $("#dryRun").checked,
  };
  withLoading("#orderForm", true);
  try {
    const data = await api("/api/orders/preview", body);
    state.currentApprovalId = data.approval.id;
    renderOrderPreview(data);
    $("#approvalBox").classList.toggle("hidden", data.approval.status !== "pending");
    $("#orderState").textContent = data.approval.status;
    showTab("orders");
    await loadLogs();
  } catch (error) {
    renderError("#orderContent", error.message);
    showTab("orders");
  } finally {
    withLoading("#orderForm", false);
  }
}

async function executeOrder() {
  if (!state.currentApprovalId) return;
  withLoading("#orderForm", true);
  try {
    const data = await api("/api/orders/execute", {
      approval_id: state.currentApprovalId,
      confirm_text: $("#confirmText").value,
    });
    renderOrderExecution(data);
    $("#approvalBox").classList.add("hidden");
    $("#orderState").textContent = "executed";
    await loadLogs();
  } catch (error) {
    renderError("#orderContent", error.message);
  } finally {
    withLoading("#orderForm", false);
  }
}

// Streamed chat: POST to the SSE endpoint and append tokens as they arrive.
async function chat(message) {
  appendBubble("user", message);
  const bubble = appendBubble("assistant", "…");
  bubble.classList.add("typing");
  withLoading("#chatForm", true);
  let acc = "";
  let firstDelta = true;
  const tools = [];
  let done = null;
  try {
    const response = await fetch("/api/chat/stream", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ message, session_id: state.sessionId }),
    });
    if (!response.ok || !response.body) {
      throw new Error(`요청 실패 (${response.status})`);
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { value, done: readerDone } = await reader.read();
      if (readerDone) break;
      buffer += decoder.decode(value, { stream: true });
      const chunks = buffer.split("\n\n");
      buffer = chunks.pop();
      for (const chunk of chunks) {
        const line = chunk.split("\n").find((l) => l.startsWith("data:"));
        if (!line) continue;
        const event = JSON.parse(line.slice(5).trim());
        if (event.type === "delta") {
          if (firstDelta) {
            firstDelta = false;
            bubble.classList.remove("typing");
            acc = "";
          }
          acc += event.text;
          setBubbleText(bubble, acc);
          $("#chatThread").scrollTop = $("#chatThread").scrollHeight;
        } else if (event.type === "tool") {
          tools.push(event.name);
          setToolTrace(bubble, tools);
        } else if (event.type === "error") {
          bubble.classList.add("error");
          setBubbleText(bubble, event.message);
        } else if (event.type === "done") {
          done = event;
        }
      }
    }
    bubble.classList.remove("typing");
    if (done) {
      const text = done.message || acc || "요청을 처리했습니다.";
      setBubbleText(bubble, text);
      const toolList = (done.tool_calls && done.tool_calls.length) ? done.tool_calls : tools;
      if (toolList.length) setToolTrace(bubble, toolList);
      if (done.session_id) {
        state.sessionId = done.session_id;
        localStorage.setItem("kis_session", state.sessionId);
      }
      renderArtifacts(done);
    }
    await loadLogs();
  } catch (error) {
    bubble.classList.remove("typing");
    bubble.classList.add("error");
    setBubbleText(bubble, error.message);
  } finally {
    withLoading("#chatForm", false);
  }
}

// Render any structured data the agent produced into the matching tab.
function renderArtifacts(response) {
  const artifacts = Array.isArray(response.artifacts) ? response.artifacts : [];
  // The keyword-fallback router returns a single `data` blob instead of artifacts.
  if (!artifacts.length && response.data) {
    artifacts.push({ type: response.kind, data: response.data });
  }
  artifacts.forEach((artifact) => {
    if (artifact.type === "analysis") {
      state.currentAnalysis = artifact.data;
      renderReport(artifact.data);
      hydrateOrderTicket(artifact.data);
    } else if (artifact.type === "screen") {
      renderScreen(artifact.data);
    } else if (artifact.type === "order") {
      state.currentApprovalId = artifact.data.approval.id;
      renderOrderPreview(artifact.data);
      $("#approvalBox").classList.toggle(
        "hidden",
        artifact.data.approval.status !== "pending",
      );
      $("#orderState").textContent = artifact.data.approval.status;
    } else if (artifact.type === "balance") {
      const sel = $("#balanceMarket");
      if (sel && artifact.data.market) sel.value = artifact.data.market;
      renderBalance(artifact.data);
    } else if (artifact.type === "executed") {
      // Auto-executed order — refresh the balance/positions view silently.
      loadBalance(false);
      loadLogs();
    }
  });
}

function appendBubble(role, text, toolCalls, isError) {
  const thread = $("#chatThread");
  const empty = thread.querySelector(".chat-empty");
  if (empty) empty.remove();
  const bubble = document.createElement("div");
  bubble.className = `bubble ${role}${isError ? " error" : ""}`;
  const textSpan = document.createElement("span");
  textSpan.className = "bubble-text";
  textSpan.textContent = text;
  bubble.appendChild(textSpan);
  if (toolCalls && toolCalls.length) setToolTrace(bubble, toolCalls);
  thread.appendChild(bubble);
  thread.scrollTop = thread.scrollHeight;
  return bubble;
}

// Update only the text node, leaving any tool-trace span intact.
function setBubbleText(bubble, text) {
  bubble.querySelector(".bubble-text").textContent = text;
}

function setToolTrace(bubble, names) {
  let trace = bubble.querySelector(".tool-trace");
  if (!trace) {
    trace = document.createElement("span");
    trace.className = "tool-trace";
    bubble.appendChild(trace);
  }
  trace.textContent = `도구 호출: ${names.join(", ")}`;
}

async function loadLogs() {
  try {
    const data = await api("/api/logs");
    $("#logsContent").innerHTML = data.logs.length
      ? data.logs.map(renderLogRow).join("")
      : `<div class="empty-state"><p>로그 없음</p></div>`;
  } catch (error) {
    renderError("#logsContent", error.message);
  }
}

function renderReport(data) {
  const q = data.quote;
  const m = data.metrics;
  const plan = data.risk_plan;
  const cur = data.currency || "KRW";
  const marketTag = data.market === "US" ? "🇺🇸 US" : "🇰🇷 KR";
  const changeClass = q.change > 0 ? "positive" : q.change < 0 ? "negative" : "neutral";
  $("#reportEmpty").classList.add("hidden");
  $("#reportContent").classList.remove("hidden");
  $("#reportContent").innerHTML = `
    <div class="hero-report">
      <article class="quote-panel">
        <div class="quote-top">
          <div>
            <div class="symbol-title">${data.symbol}</div>
            <p class="neutral">${escapeHtml(data.name)} · ${marketTag}</p>
          </div>
          <span class="badge ${badgeClass(data.action.tone)}">${data.action.text}</span>
        </div>
        <div class="price">${money(q.price, cur)}</div>
        <div class="change ${changeClass}">${signedMoney(q.change, cur)} · ${signed(q.change_pct)}%</div>
      </article>
      <article class="risk-panel">
        <div class="panel-head"><h2>리스크 플랜</h2><span class="mini-state">${data.score}점</span></div>
        <div class="risk-grid">
          ${riskItem("권장진입", money(plan.entry_reference, cur))}
          ${riskItem("손절", money(plan.stop_loss, cur))}
          ${riskItem("목표", money(plan.take_profit, cur))}
          ${riskItem("권장수량", `${fmt.format(plan.suggested_quantity)}주`)}
        </div>
      </article>
    </div>

    <section class="section-block">
      <div class="panel-head">
        <h2>차트 (일봉)</h2>
        <div class="chart-controls">
          <div class="period-toggle">
            <button type="button" data-period="1M">1M</button>
            <button type="button" data-period="3M">3M</button>
            <button type="button" data-period="6M">6M</button>
            <button type="button" data-period="1Y">1Y</button>
            <button type="button" data-period="5Y">5Y</button>
            <button type="button" data-period="10Y">10Y</button>
            <button type="button" data-period="MAX">최대</button>
          </div>
          <label class="bb-toggle"><input type="checkbox" id="bbToggle" /> 볼린저밴드</label>
        </div>
      </div>
      <div id="chartArea" class="chart-area"></div>
      <div id="rsiArea"></div>
      <div class="chart-legend">
        <span style="color:#e8890c">━ SMA5</span>
        <span style="color:#7c4dff">━ SMA20</span>
        <span style="color:#16845b">━ SMA60</span>
        <span class="neutral">빨강=상승·파랑=하락 · 캔들에 커서를 올리면 상세</span>
      </div>
    </section>

    <section class="section-block">
      <div class="panel-head"><h2>지표</h2><span class="mini-state">${data.environment}</span></div>
      <div class="metric-grid">
        ${metric("SMA5", money(m.sma5, cur))}
        ${metric("SMA20", money(m.sma20, cur))}
        ${metric("SMA60", money(m.sma60, cur))}
        ${metric("RSI14", numberValue(m.rsi14))}
        ${metric("ATR14", money(m.atr14, cur))}
        ${metric("거래량", `${numberValue(m.volume_ratio20)}x`)}
        ${metric("20일 수익률", `${signed(m.return20d_pct)}%`)}
        ${metric("20일 저가", money(m.low20, cur))}
      </div>
    </section>

    <section class="section-block">
      <div class="panel-head"><h2>시그널</h2><span class="mini-state">${data.signals.length}</span></div>
      <div class="signal-list">
        ${
          data.signals.length
            ? data.signals.map(renderSignal).join("")
            : `<div class="signal"><strong>중립</strong><p class="neutral">뚜렷한 시그널이 없습니다.</p></div>`
        }
      </div>
    </section>
  `;
  if (window.lucide) window.lucide.createIcons();
  // Seed the chart cache from the analysis window (~6M). Longer periods fetch more.
  const key = `${data.symbol}:${data.market}`;
  if (state.chartKey !== key) state.chartPeriod = state.chartPeriod || "3M";
  state.chartBars = Array.isArray(data.recent_bars) ? data.recent_bars : [];
  state.chartReqDays = PERIOD_DAYS["6M"];
  state.chartKey = key;
  setupChartControls();
  drawReportChart();
}

// Downsample to keep long ranges (5Y/10Y/MAX) fast: aggregate into OHLCV buckets.
function aggregateBars(bars, maxOut) {
  if (!Array.isArray(bars) || bars.length <= maxOut) return bars;
  const k = Math.ceil(bars.length / maxOut);
  const out = [];
  for (let i = 0; i < bars.length; i += k) {
    const chunk = bars.slice(i, i + k);
    out.push({
      date: chunk[chunk.length - 1].date,
      open: chunk[0].open,
      close: chunk[chunk.length - 1].close,
      high: Math.max(...chunk.map((b) => b.high)),
      low: Math.min(...chunk.map((b) => b.low)),
      volume: chunk.reduce((s, b) => s + b.volume, 0),
    });
  }
  return out;
}

// ---- Indicator math (client-side) -----------------------------------------
function smaSeries(closes, period) {
  const out = new Array(closes.length).fill(null);
  let sum = 0;
  for (let i = 0; i < closes.length; i++) {
    sum += closes[i];
    if (i >= period) sum -= closes[i - period];
    if (i >= period - 1) out[i] = sum / period;
  }
  return out;
}

function bollingerSeries(closes, period = 20, mult = 2) {
  const mid = smaSeries(closes, period);
  const upper = new Array(closes.length).fill(null);
  const lower = new Array(closes.length).fill(null);
  for (let i = period - 1; i < closes.length; i++) {
    let s = 0;
    for (let j = i - period + 1; j <= i; j++) s += (closes[j] - mid[i]) ** 2;
    const sd = Math.sqrt(s / period);
    upper[i] = mid[i] + mult * sd;
    lower[i] = mid[i] - mult * sd;
  }
  return { mid, upper, lower };
}

function rsiSeries(closes, period = 14) {
  const out = new Array(closes.length).fill(null);
  if (closes.length < period + 1) return out;
  let g = 0, l = 0;
  for (let i = 1; i <= period; i++) {
    const d = closes[i] - closes[i - 1];
    g += Math.max(d, 0);
    l += Math.max(-d, 0);
  }
  let ag = g / period, al = l / period;
  out[period] = al === 0 ? 100 : 100 - 100 / (1 + ag / al);
  for (let i = period + 1; i < closes.length; i++) {
    const d = closes[i] - closes[i - 1];
    ag = (ag * (period - 1) + Math.max(d, 0)) / period;
    al = (al * (period - 1) + Math.max(-d, 0)) / period;
    out[i] = al === 0 ? 100 : 100 - 100 / (1 + ag / al);
  }
  return out;
}

function polyline(xs, ys, series, color, width = 1.5, dash) {
  const pts = [];
  for (let i = 0; i < series.length; i++) {
    if (series[i] == null) continue;
    pts.push(`${xs(i)},${ys(series[i])}`);
  }
  if (pts.length < 2) return "";
  return `<polyline points="${pts.join(" ")}" fill="none" stroke="${color}" stroke-width="${width}"${dash ? ` stroke-dasharray="${dash}"` : ""}/>`;
}

// ---- Price candlestick chart (candles + SMA + Bollinger + volume + hover) --
function buildPriceSVG(bars, currency, opts) {
  const W = 760, H = 320, padL = 64, padR = 12, padT = 10, padB = 20, volH = 56;
  const priceH = H - padT - padB - volH - 8;
  const n = bars.length;
  const closes = bars.map((b) => b.close);
  const bb = opts.showBB ? bollingerSeries(closes, 20, 2) : null;
  let maxP = Math.max(...bars.map((b) => b.high));
  let minP = Math.min(...bars.map((b) => b.low));
  if (bb) {
    bb.upper.forEach((v) => v != null && (maxP = Math.max(maxP, v)));
    bb.lower.forEach((v) => v != null && (minP = Math.min(minP, v)));
  }
  const maxV = Math.max(...bars.map((b) => b.volume), 1);
  const slot = (W - padL - padR) / n;
  const cw = Math.max(1, Math.min(9, slot * 0.62));
  const x = (i) => padL + slot * (i + 0.5);
  const yP = (p) => padT + priceH * (1 - (p - minP) / (maxP - minP || 1));
  const volTop = padT + priceH + 8;
  const yV = (v) => volTop + volH * (1 - v / maxV);
  const col = (b) => (b.close >= b.open ? "var(--red)" : "var(--blue)");
  const axisPrice = (p) => (currency === "USD" ? `$${usdFmt.format(p)}` : fmt.format(Math.round(p)));
  const dateLabel = (s) => (s && s.length >= 8 ? `${s.slice(4, 6)}/${s.slice(6, 8)}` : "");

  let grid = "";
  [maxP, (maxP + minP) / 2, minP].forEach((p) => {
    grid += `<line x1="${padL}" y1="${yP(p)}" x2="${W - padR}" y2="${yP(p)}" stroke="var(--line)" stroke-width="0.5"/>`;
    grid += `<text x="${padL - 6}" y="${yP(p) + 3}" text-anchor="end" class="chart-axis">${axisPrice(p)}</text>`;
  });
  [0, Math.floor(n / 3), Math.floor((2 * n) / 3), n - 1].forEach((i) => {
    grid += `<text x="${x(i)}" y="${H - 5}" text-anchor="middle" class="chart-axis">${dateLabel(bars[i].date)}</text>`;
  });

  let bbLayer = "";
  if (bb) {
    bbLayer =
      polyline(x, yP, bb.upper, "#9aa7b8", 1, "3 3") +
      polyline(x, yP, bb.lower, "#9aa7b8", 1, "3 3") +
      polyline(x, yP, bb.mid, "#9aa7b8", 1);
  }

  let candles = "", vols = "", zones = "";
  bars.forEach((b, i) => {
    const c = col(b), cx = x(i);
    candles += `<line x1="${cx}" y1="${yP(b.high)}" x2="${cx}" y2="${yP(b.low)}" stroke="${c}" stroke-width="1"/>`;
    const top = Math.min(yP(b.open), yP(b.close));
    candles += `<rect x="${cx - cw / 2}" y="${top}" width="${cw}" height="${Math.max(1, Math.abs(yP(b.close) - yP(b.open)))}" fill="${c}"/>`;
    vols += `<rect x="${cx - cw / 2}" y="${yV(b.volume)}" width="${cw}" height="${volTop + volH - yV(b.volume)}" fill="${c}" opacity="0.45"/>`;
    zones += `<rect class="hover-zone" data-i="${i}" x="${padL + slot * i}" y="${padT}" width="${slot}" height="${priceH + 8 + volH}" fill="transparent"/>`;
  });

  const sma = (p, color) => polyline(x, yP, smaSeries(closes, p), color);
  return {
    svg: `<svg viewBox="0 0 ${W} ${H}" class="price-chart" preserveAspectRatio="xMidYMid meet" role="img" aria-label="일봉 차트">
      ${grid}${bbLayer}${vols}${candles}${sma(5, "#e8890c")}${sma(20, "#7c4dff")}${sma(60, "#16845b")}${zones}
    </svg>`,
  };
}

function buildRSISVG(bars) {
  const W = 760, H = 90, padL = 64, padR = 12, padT = 8, padB = 14;
  const closes = bars.map((b) => b.close);
  const rsi = rsiSeries(closes, 14);
  const n = bars.length;
  const x = (i) => padL + ((W - padL - padR) / n) * (i + 0.5);
  const y = (v) => padT + (H - padT - padB) * (1 - v / 100);
  let g = "";
  [70, 50, 30].forEach((lvl) => {
    g += `<line x1="${padL}" y1="${y(lvl)}" x2="${W - padR}" y2="${y(lvl)}" stroke="var(--line)" stroke-width="0.5" ${lvl !== 50 ? 'stroke-dasharray="3 3"' : ""}/>`;
    g += `<text x="${padL - 6}" y="${y(lvl) + 3}" text-anchor="end" class="chart-axis">${lvl}</text>`;
  });
  return `<svg viewBox="0 0 ${W} ${H}" class="rsi-chart" preserveAspectRatio="xMidYMid meet" aria-label="RSI">
    ${g}${polyline(x, y, rsi, "#1f6feb", 1.5)}
    <text x="${padL}" y="${padT + 6}" class="chart-axis">RSI(14)</text>
  </svg>`;
}

function mountPriceChart(container, bars, currency, opts) {
  const built = buildPriceSVG(bars, currency, opts);
  container.innerHTML = built.svg + `<div class="chart-tooltip hidden"></div>`;
  const svgEl = container.querySelector("svg");
  const tip = container.querySelector(".chart-tooltip");
  svgEl.querySelectorAll(".hover-zone").forEach((z) => {
    z.addEventListener("mouseenter", () => {
      const b = bars[Number(z.dataset.i)];
      const up = b.close >= b.open;
      tip.innerHTML =
        `<strong>${b.date.slice(4, 6)}/${b.date.slice(6, 8)}</strong> ` +
        `시 ${money(b.open, currency)} · 고 ${money(b.high, currency)} · 저 ${money(b.low, currency)} · ` +
        `종 <b class="${up ? "positive" : "negative"}">${money(b.close, currency)}</b> · 거래량 ${fmt.format(b.volume)}`;
      tip.classList.remove("hidden");
      const cr = container.getBoundingClientRect();
      const zr = z.getBoundingClientRect();
      tip.style.left = `${Math.min(Math.max(0, zr.left - cr.left - 90), Math.max(0, cr.width - 240))}px`;
    });
  });
  svgEl.addEventListener("mouseleave", () => tip.classList.add("hidden"));
}

function drawReportChart() {
  const data = state.currentAnalysis;
  const area = $("#chartArea");
  if (!data || !area) return;
  const all = state.chartBars && state.chartBars.length ? state.chartBars : data.recent_bars || [];
  if (!all.length) return;
  const cur = data.currency || "KRW";
  const count = PERIOD_BARS[state.chartPeriod] || all.length;
  const sliced = all.slice(-Math.min(count, all.length));
  const bars = aggregateBars(sliced, 600); // cap drawn candles for performance
  mountPriceChart(area, bars, cur, { showBB: state.chartShowBB });
  const rsiEl = $("#rsiArea");
  if (rsiEl) rsiEl.innerHTML = buildRSISVG(bars);
  setActivePeriodButton(state.chartPeriod);
}

function setActivePeriodButton(period) {
  document.querySelectorAll("#reportContent .period-toggle button").forEach((b) =>
    b.classList.toggle("active", b.dataset.period === period),
  );
}

// Switch chart period; fetch deeper history on demand (only when we don't already
// have enough). Recently-listed names just return fewer bars — no refetch loop.
async function selectPeriod(period) {
  state.chartPeriod = period;
  setActivePeriodButton(period);
  const data = state.currentAnalysis;
  const needDays = PERIOD_DAYS[period] || PERIOD_DAYS["6M"];
  if (data && needDays > state.chartReqDays) {
    const area = $("#chartArea");
    if (area) area.innerHTML = `<p class="neutral" style="padding:24px">📈 히스토리 불러오는 중…</p>`;
    try {
      const res = await api(
        `/api/prices?symbol=${encodeURIComponent(data.symbol)}&market=${encodeURIComponent(data.market)}&days=${needDays}`,
      );
      if (res && Array.isArray(res.bars) && res.bars.length) {
        state.chartBars = res.bars;
        state.chartReqDays = needDays; // mark coverage so we don't refetch shorter ranges
      }
    } catch (error) {
      // Keep whatever bars we already have; just redraw.
    }
  }
  drawReportChart();
}

function setupChartControls() {
  document.querySelectorAll("#reportContent .period-toggle button").forEach((b) =>
    b.addEventListener("click", () => selectPeriod(b.dataset.period)),
  );
  const bb = $("#bbToggle");
  if (bb) {
    bb.checked = state.chartShowBB;
    bb.addEventListener("change", () => {
      state.chartShowBB = bb.checked;
      drawReportChart();
    });
  }
}

// ---- Comparison chart (scan tab): normalized % lines ----------------------
function buildCompareSVG(results, count) {
  const series = results
    .filter((r) => Array.isArray(r.recent_bars) && r.recent_bars.length > 2)
    .map((r, idx) => {
      const closes = r.recent_bars.slice(-count).map((b) => b.close);
      const base = closes[0];
      return {
        symbol: r.symbol,
        name: r.name,
        color: COMPARE_COLORS[idx % COMPARE_COLORS.length],
        pct: closes.map((c) => (c / base - 1) * 100),
      };
    });
  if (!series.length) return "";
  const W = 760, H = 300, padL = 50, padR = 12, padT = 10, padB = 20;
  const n = Math.max(...series.map((s) => s.pct.length));
  let lo = 0, hi = 0;
  series.forEach((s) => s.pct.forEach((v) => { lo = Math.min(lo, v); hi = Math.max(hi, v); }));
  const pad = (hi - lo) * 0.1 || 1;
  lo -= pad; hi += pad;
  const x = (i) => padL + ((W - padL - padR) / Math.max(1, n - 1)) * i;
  const y = (v) => padT + (H - padT - padB) * (1 - (v - lo) / (hi - lo || 1));
  let grid = "";
  [hi, 0, lo].forEach((v) => {
    grid += `<line x1="${padL}" y1="${y(v)}" x2="${W - padR}" y2="${y(v)}" stroke="var(--line)" stroke-width="${v === 0 ? 1 : 0.5}" ${v === 0 ? "" : 'stroke-dasharray="3 3"'}/>`;
    grid += `<text x="${padL - 6}" y="${y(v) + 3}" text-anchor="end" class="chart-axis">${v >= 0 ? "+" : ""}${v.toFixed(1)}%</text>`;
  });
  const lines = series
    .map((s) => polyline((i) => x(i), (v) => y(v), s.pct, s.color, 1.8))
    .join("");
  const legend = series
    .map((s) => {
      const last = s.pct[s.pct.length - 1];
      return `<span style="color:${s.color}">━ ${escapeHtml(s.name || s.symbol)} <b>${last >= 0 ? "+" : ""}${last.toFixed(1)}%</b></span>`;
    })
    .join("");
  return `
    <section class="section-block">
      <div class="panel-head"><h2>종목 비교 (기간 수익률)</h2><span class="mini-state">${series.length}종목</span></div>
      <svg viewBox="0 0 ${W} ${H}" class="price-chart" preserveAspectRatio="xMidYMid meet" aria-label="비교 차트">${grid}${lines}</svg>
      <div class="chart-legend">${legend}</div>
    </section>`;
}

function renderScreen(data) {
  const compare = buildCompareSVG(data.results || [], 66);
  const rows = data.results
    .map((item) => {
      const q = item.quote;
      return `
        <tr>
          <td><strong>${item.symbol}</strong></td>
          <td>${escapeHtml(item.name)}</td>
          <td>${money(q.price, item.currency)}</td>
          <td class="${q.change >= 0 ? "positive" : "negative"}">${signed(q.change_pct)}%</td>
          <td>${item.score}</td>
          <td><span class="badge ${badgeClass(item.action.tone)}">${item.action.text}</span></td>
        </tr>`;
    })
    .join("");
  const errors = data.errors.length
    ? `<div class="risk-list">${data.errors.map((e) => `<div class="risk-message">${escapeHtml(e.symbol)}: ${escapeHtml(e.error)}</div>`).join("")}</div>`
    : "";
  $("#screenContent").innerHTML = `
    ${compare}
    <section class="section-block">
      <div class="panel-head"><h2>스캔 결과</h2><span class="mini-state">${(data.results || []).length}종목</span></div>
      <div class="table-wrap"><table>
        <thead>
          <tr><th>종목</th><th>이름</th><th>현재가</th><th>등락률</th><th>점수</th><th>판단</th></tr>
        </thead>
        <tbody>${rows}</tbody>
      </table></div>
    </section>
    ${errors}`;
}

function renderOrderPreview(data) {
  const approval = data.approval;
  const check = approval.risk_check;
  $("#orderContent").innerHTML = `
    <section class="section-block">
      <div class="panel-head">
        <h2>주문 검증</h2>
        <span class="badge ${check.allowed ? "" : "danger"}">${approval.status}</span>
      </div>
      ${orderSummary(approval.draft)}
    </section>
    ${riskMessages("차단", check.blocks, "danger")}
    ${riskMessages("경고", check.warnings, "caution")}`;
}

function renderOrderExecution(data) {
  $("#orderContent").innerHTML = `
    <section class="section-block">
      <div class="panel-head"><h2>주문 결과</h2><span class="badge">executed</span></div>
      ${orderSummary(data.approval.draft)}
      <pre>${escapeHtml(JSON.stringify(data.broker_response, null, 2))}</pre>
    </section>`;
}

function hydrateOrderTicket(data) {
  $("#orderSymbol").value = data.symbol;
  // Prefill the limit price with the suggested (pullback) entry, not the current price.
  $("#orderPrice").value = (data.risk_plan && data.risk_plan.entry_reference) || data.quote.price;
  const plan = data.risk_plan;
  // Prefer the risk-based conservative size; fall back to the order-limit cap.
  $("#orderQty").value = Math.max(
    1,
    plan.suggested_quantity || plan.max_quantity_by_order_limit || 1,
  );
}

function renderBalance(data) {
  const p = data.agent_portfolio || {};
  const cur = data.currency || p.currency || "KRW";
  const positions = p.positions || [];
  const isBroker = p.source === "broker";
  const sourceTag = isBroker
    ? `<span class="badge">🔗 실제 KIS 계좌</span>`
    : `<span class="badge muted">🧪 시뮬레이션</span>`;
  const rows = positions
    .map(
      (pos) => `
        <tr>
          <td><strong>${escapeHtml(pos.symbol)}</strong>${pos.name ? ` <span class="neutral">${escapeHtml(pos.name)}</span>` : ""}</td>
          <td>${fmt.format(pos.qty)}</td>
          <td>${money(pos.avg_cost, cur)}</td>
          <td>${money(pos.price, cur)}</td>
          <td>${money(pos.market_value, cur)}</td>
          <td class="${pos.unrealized_pnl >= 0 ? "positive" : "negative"}">${signedMoney(pos.unrealized_pnl, cur)} · ${signed(pos.unrealized_pct)}%</td>
        </tr>`,
    )
    .join("");
  const table = positions.length
    ? `<section class="section-block">
        <div class="panel-head"><h2>보유 종목</h2><span class="mini-state">${positions.length}</span></div>
        <div class="table-wrap"><table>
          <thead><tr><th>종목</th><th>수량</th><th>평단</th><th>현재가</th><th>평가금액</th><th>평가손익</th></tr></thead>
          <tbody>${rows}</tbody>
        </table></div>
      </section>`
    : `<div class="empty-state"><p>보유 종목이 없습니다.</p></div>`;
  $("#balanceContent").innerHTML = `
    <div class="balance-source">${sourceTag}</div>
    <div class="metric-grid">
      ${metric("평가자산", money(p.equity, cur))}
      ${metric("현금(예수금)", money(p.cash, cur))}
      ${metric("실현손익(오늘)", p.realized_pnl_today == null ? "—" : signedMoney(p.realized_pnl_today, cur))}
      ${metric("평가손익", signedMoney(p.unrealized_pnl, cur))}
    </div>
    ${table}`;
}

function orderSummary(draft) {
  const cur = currencyOf(draft.market);
  return `
    <div class="metric-grid">
      ${metric("종목", `${draft.symbol} (${draft.market === "US" ? "US" : "KR"})`)}
      ${metric("구분", draft.side === "buy" ? "매수" : "매도")}
      ${metric("수량", `${fmt.format(draft.quantity)}주`)}
      ${metric("지정가", money(draft.limit_price, cur))}
      ${metric("금액", money(draft.quantity * draft.limit_price, cur))}
    </div>`;
}

function riskMessages(title, messages, tone) {
  if (!messages.length) return "";
  return `
    <div class="section-block">
      <div class="panel-head"><h2>${title}</h2></div>
      <div class="risk-list">
        ${messages.map((message) => `<div class="risk-message ${tone}">${escapeHtml(message)}</div>`).join("")}
      </div>
    </div>`;
}

function renderSignal(signal) {
  const tone = signal.impact > 0 ? "positive" : signal.impact < 0 ? "negative" : "neutral";
  return `
    <div class="signal">
      <div class="signal-header"><strong>${escapeHtml(signal.name)}</strong><span class="${tone}">${signed(signal.impact)}</span></div>
      <p>${escapeHtml(signal.detail)}</p>
    </div>`;
}

function renderLogRow(log) {
  return `
    <div class="log-row">
      <div class="log-meta"><span>${escapeHtml(log.type)}</span><span>${escapeHtml(log.timestamp)}</span></div>
      <pre>${escapeHtml(JSON.stringify(log.payload, null, 2))}</pre>
    </div>`;
}

function renderError(selector, message) {
  $(selector).innerHTML = `<div class="risk-message danger">${escapeHtml(message)}</div>`;
}

function renderJson(selector, data) {
  $(selector).innerHTML = `<pre>${escapeHtml(JSON.stringify(data, null, 2))}</pre>`;
}

function showTab(name) {
  document.querySelectorAll(".tab").forEach((tab) => tab.classList.toggle("active", tab.dataset.tab === name));
  document.querySelectorAll(".tab-panel").forEach((panel) => panel.classList.remove("active"));
  $(`#${name}Tab`).classList.add("active");
}

async function api(path, body) {
  const headers = {};
  if (state.dashboardToken) headers["X-Dashboard-Token"] = state.dashboardToken;
  const options = body
    ? {
        method: "POST",
        headers: { ...headers, "content-type": "application/json" },
        body: JSON.stringify(body),
      }
    : Object.keys(headers).length
      ? { headers }
      : {};
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) {
    throw new Error(data.detail || "요청 실패");
  }
  return data;
}

function withLoading(selector, enabled) {
  $(selector).classList.toggle("loading", enabled);
}

function metric(label, value) {
  return `<div class="metric"><span>${label}</span><strong>${value ?? "-"}</strong></div>`;
}

function riskItem(label, value) {
  return `<div class="risk-item"><span>${label}</span><strong>${value ?? "-"}</strong></div>`;
}

const usdFmt = new Intl.NumberFormat("en-US", {
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

// Currency-aware amount formatter (₩ for KRW, $ for USD). Defaults to KRW.
function money(value, currency) {
  if (value === null || value === undefined) return "-";
  if (currency === "USD") return `$${usdFmt.format(value)}`;
  return `${fmt.format(value)}원`;
}

function signedMoney(value, currency) {
  const n = Number(value || 0);
  const sign = n > 0 ? "+" : n < 0 ? "-" : "";
  if (currency === "USD") return `${sign}$${usdFmt.format(Math.abs(n))}`;
  return `${sign}${fmt.format(Math.abs(n))}원`;
}

function numberValue(value) {
  return value === null || value === undefined ? "-" : fmt.format(value);
}

function signed(value) {
  const number = Number(value || 0);
  return `${number > 0 ? "+" : ""}${fmt.format(number)}`;
}

function currencyOf(market) {
  return market === "US" ? "USD" : "KRW";
}

function badgeClass(tone) {
  if (tone === "positive") return "";
  if (tone === "caution") return "danger";
  return "muted";
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}
