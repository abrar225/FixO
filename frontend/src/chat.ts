/**
 * JARVIS Active Chat Panel (Claude Code & Gemini Web Experience)
 *
 * Real-time conversation stream:
 * - Multi-Session Drawer (New chat, switch historical sessions)
 * - Rich Markdown & Syntax-Highlighted Code Blocks with Copy & Download
 * - Live HTML/CSS/JS Sandbox Preview for generated code artifacts
 * - Structured Flight & Route Cards with real-time indicators
 * - Integrated Command Input & Knowledge Graph memory tracker
 */

export interface ChatMessage {
  id: string;
  session_id?: string;
  timestamp: string;
  role: "user" | "jarvis";
  text: string;
  action?: { action: string; target?: string } | null;
  model?: string;
  latency_ms?: number;
}

export interface ChatSession {
  id: string;
  title: string;
  created_at: number;
  updated_at: number;
  formatted_date: string;
  summary: string;
  message_count: number;
  last_message: string;
  is_active: boolean;
}

let chatContainer: HTMLElement | null = null;
let isOpen = false;
let isSidebarOpen = false;
const messages: ChatMessage[] = [];
let currentSessionId: string | null = null;
let sessionsList: ChatSession[] = [];

export function initChatPanel(onSendMessage?: (text: string) => void) {
  if (document.getElementById("chat-panel-container")) return;

  chatContainer = document.createElement("div");
  chatContainer.id = "chat-panel-container";
  chatContainer.innerHTML = `
    <div class="chat-backdrop" id="chat-backdrop"></div>
    <div class="chat-panel">
      
      <!-- Multi-Session Drawer / Sidebar -->
      <div class="chat-sidebar" id="chat-sidebar">
        <div class="chat-sidebar-header">
          <div class="chat-sidebar-title">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"></path>
            </svg>
            <span>Chat Sessions</span>
          </div>
          <button class="chat-sidebar-new" id="chat-sidebar-new-btn" title="New Session">+ New</button>
        </div>
        <div class="chat-sidebar-list" id="chat-sidebar-list">
          <div class="chat-sidebar-empty">Loading sessions...</div>
        </div>
        <div class="chat-sidebar-footer" id="chat-sidebar-graph-stats">
          <div class="graph-stat-pill">🧠 Knowledge Graph Active</div>
        </div>
      </div>

      <!-- Main Chat Area -->
      <div class="chat-main-area">
        <div class="chat-header">
          <div class="chat-title-group">
            <button class="chat-sidebar-toggle" id="chat-sidebar-toggle" title="Toggle Sessions History">
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <line x1="3" y1="12" x2="21" y2="12"></line>
                <line x1="3" y1="6" x2="21" y2="6"></line>
                <line x1="3" y1="18" x2="21" y2="18"></line>
              </svg>
            </button>
            <div class="chat-orb-mini"></div>
            <div class="chat-header-text">
              <h2>FixO Command & Intelligence Log</h2>
              <span class="chat-session-subtitle" id="chat-session-subtitle">Active Session</span>
            </div>
          </div>
          <div class="chat-header-actions">
            <button class="chat-header-btn" id="chat-new-chat-btn" title="New Conversation">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <line x1="12" y1="5" x2="12" y2="19"></line>
                <line x1="5" y1="12" x2="19" y2="12"></line>
              </svg>
              <span>New</span>
            </button>
            <button class="chat-close" id="chat-close" title="Close Chat">✕</button>
          </div>
        </div>

        <div class="chat-messages" id="chat-messages">
          <div class="chat-empty" id="chat-empty">
            <div class="chat-empty-icon">⚡</div>
            <h3>J.A.R.V.I.S. Ready</h3>
            <p>Speak or type to generate code, plan trips, run deep research, or control macOS.</p>
          </div>
        </div>

        <div class="chat-input-container">
          <input 
            type="text" 
            id="chat-input-box" 
            placeholder="Type or speak a request (e.g. 'design animated 3D card in HTML', 'flights to Dubai')..." 
            autocomplete="off"
          />
          <button id="chat-send-btn" title="Send Command">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <line x1="22" y1="2" x2="11" y2="13"></line>
              <polygon points="22 2 15 22 11 13 2 9 22 2"></polygon>
            </svg>
          </button>
        </div>
      </div>
    </div>

    <!-- Live HTML/CSS Sandbox Preview Modal -->
    <div class="chat-preview-modal" id="chat-preview-modal">
      <div class="chat-preview-header">
        <div class="chat-preview-title">
          <span class="preview-dot"></span>
          <h3>Live Code Artifact Preview</h3>
        </div>
        <button class="chat-preview-close" id="chat-preview-close">✕</button>
      </div>
      <div class="chat-preview-body">
        <iframe id="chat-preview-iframe" sandbox="allow-scripts allow-modals"></iframe>
      </div>
    </div>
  `;

  document.body.appendChild(chatContainer);

  document.getElementById("chat-close")?.addEventListener("click", () => toggleChatPanel());
  document.getElementById("chat-backdrop")?.addEventListener("click", () => toggleChatPanel());
  document.getElementById("chat-sidebar-toggle")?.addEventListener("click", () => toggleSidebar());
  document.getElementById("chat-sidebar-new-btn")?.addEventListener("click", () => startNewSession());
  document.getElementById("chat-new-chat-btn")?.addEventListener("click", () => startNewSession());
  document.getElementById("chat-preview-close")?.addEventListener("click", () => closePreviewModal());

  const inputBox = document.getElementById("chat-input-box") as HTMLInputElement;
  const sendBtn = document.getElementById("chat-send-btn");

  const handleSend = () => {
    const text = inputBox.value.trim();
    if (!text) return;
    inputBox.value = "";
    if (onSendMessage) {
      onSendMessage(text);
    } else {
      sendTypedMessage(text);
    }
  };

  sendBtn?.addEventListener("click", handleSend);
  inputBox?.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  });

  // Setup Global Event Delegation for Code Copy, Download & Preview
  document.getElementById("chat-messages")?.addEventListener("click", (e) => {
    const target = e.target as HTMLElement;
    const btn = target.closest(".code-action-btn") as HTMLButtonElement;
    if (!btn) return;

    const action = btn.dataset.action;
    const codeId = btn.dataset.target;
    const codeEl = document.getElementById(codeId || "");
    const codeContent = codeEl ? codeEl.innerText : "";

    if (action === "copy" && codeContent) {
      navigator.clipboard.writeText(codeContent);
      btn.innerHTML = `✓ Copied`;
      setTimeout(() => {
        btn.innerHTML = `Copy`;
      }, 2000);
    } else if (action === "preview" && codeContent) {
      openPreviewModal(codeContent);
    } else if (action === "download" && codeContent) {
      downloadCodeFile(codeContent, btn.dataset.lang || "txt");
    }
  });

  // Fetch initial history & sessions
  fetchChatHistory();
  fetchSessionsList();
  fetchGraphStats();
}

export async function fetchChatHistory(sessionId?: string) {
  try {
    const url = sessionId ? `/api/chat/history?session_id=${encodeURIComponent(sessionId)}` : "/api/chat/history";
    const res = await fetch(url);
    if (!res.ok) return;
    const data = await res.json();
    if (data.messages && Array.isArray(data.messages)) {
      messages.length = 0;
      data.messages.forEach((m: ChatMessage) => messages.push(m));
      currentSessionId = data.session_id || sessionId || null;
      updateSessionSubtitle();
      renderMessages();
    }
  } catch (err) {
    console.error("Failed to load chat history:", err);
  }
}

export async function fetchSessionsList() {
  try {
    const res = await fetch("/api/chat/sessions");
    if (!res.ok) return;
    const data = await res.json();
    if (data.sessions && Array.isArray(data.sessions)) {
      sessionsList = data.sessions;
      renderSessionsList();
    }
  } catch (err) {
    console.error("Failed to load sessions:", err);
  }
}

export async function fetchGraphStats() {
  try {
    const res = await fetch("/api/graph/stats");
    if (!res.ok) return;
    const data = await res.json();
    const statsEl = document.getElementById("chat-sidebar-graph-stats");
    if (statsEl) {
      statsEl.innerHTML = `
        <div class="graph-stat-pill">
          <span>🧠 Graph:</span> <strong>${data.nodes || 0} Nodes</strong> · <strong>${data.edges || 0} Edges</strong>
        </div>
      `;
    }
  } catch (err) {
    console.error("Failed to load graph stats:", err);
  }
}

export function renderSessionsList() {
  const listEl = document.getElementById("chat-sidebar-list");
  if (!listEl) return;

  if (sessionsList.length === 0) {
    listEl.innerHTML = `<div class="chat-sidebar-empty">No past sessions found.</div>`;
    return;
  }

  listEl.innerHTML = sessionsList
    .map((s) => {
      const activeClass = s.id === currentSessionId ? "active" : "";
      return `
        <div class="chat-session-item ${activeClass}" data-session-id="${s.id}">
          <div class="chat-session-title">${escapeHtml(s.title || "Conversation")}</div>
          <div class="chat-session-meta">
            <span class="chat-session-date">${s.formatted_date || ""}</span>
            <span class="chat-session-count">${s.message_count} msgs</span>
          </div>
        </div>
      `;
    })
    .join("");

  // Attach switch click listeners
  listEl.querySelectorAll(".chat-session-item").forEach((el) => {
    el.addEventListener("click", () => {
      const sid = el.getAttribute("data-session-id");
      if (sid && sid !== currentSessionId) {
        switchSession(sid);
      }
    });
  });
}

export async function switchSession(sessionId: string) {
  try {
    const res = await fetch("/api/chat/sessions/switch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: sessionId }),
    });
    if (res.ok) {
      currentSessionId = sessionId;
      await fetchChatHistory(sessionId);
      await fetchSessionsList();
      if (window.innerWidth < 768) {
        toggleSidebar(false);
      }
    }
  } catch (err) {
    console.error("Failed to switch session:", err);
  }
}

export async function startNewSession() {
  try {
    const res = await fetch("/api/chat/sessions/new", { method: "POST" });
    if (res.ok) {
      const data = await res.json();
      currentSessionId = data.session_id;
      messages.length = 0;
      renderMessages();
      await fetchSessionsList();
      updateSessionSubtitle();
      if (window.innerWidth < 768) {
        toggleSidebar(false);
      }
    }
  } catch (err) {
    console.error("Failed to start new session:", err);
  }
}

export function updateSessionSubtitle() {
  const sub = document.getElementById("chat-session-subtitle");
  if (!sub) return;
  const curr = sessionsList.find((s) => s.id === currentSessionId);
  sub.textContent = curr ? curr.title : "Active Conversation";
}

export function toggleSidebar(forceState?: boolean) {
  const sidebar = document.getElementById("chat-sidebar");
  isSidebarOpen = typeof forceState === "boolean" ? forceState : !isSidebarOpen;
  if (sidebar) {
    if (isSidebarOpen) {
      sidebar.classList.add("open");
    } else {
      sidebar.classList.remove("open");
    }
  }
}

export function addChatMessage(msg: ChatMessage) {
  // Prevent duplicate by id
  if (!messages.some((m) => m.id === msg.id)) {
    messages.push(msg);
    renderMessages();
    fetchSessionsList();
    fetchGraphStats();
  }
}

export function renderMessages() {
  const container = document.getElementById("chat-messages");
  const emptyState = document.getElementById("chat-empty");
  if (!container) return;

  if (messages.length === 0) {
    if (emptyState) emptyState.style.display = "flex";
    container.innerHTML = "";
    container.appendChild(emptyState!);
    return;
  }

  if (emptyState) emptyState.style.display = "none";

  let codeCounter = 0;
  container.innerHTML = messages
    .map((msg) => {
      const isUser = msg.role === "user";
      const actionBadge = msg.action
        ? `<div class="chat-action-badge">⚡ Action: <strong>${escapeHtml(msg.action.action.toUpperCase())}</strong> ${
            msg.action.target ? `(${escapeHtml(msg.action.target)})` : ""
          }</div>`
        : "";
      const latencyBadge = msg.latency_ms
        ? `<span class="chat-latency">${msg.latency_ms.toFixed(0)}ms</span>`
        : "";
      const modelBadge = msg.model
        ? `<span class="chat-model">${escapeHtml(msg.model.replace("ollama/", ""))}</span>`
        : "";

      const formattedContent = isUser ? escapeHtml(msg.text) : formatChatMarkdown(msg.text, () => ++codeCounter);

      return `
        <div class="chat-bubble-row ${isUser ? "row-user" : "row-jarvis"}">
          <div class="chat-bubble ${isUser ? "bubble-user" : "bubble-jarvis"}">
            <div class="chat-bubble-header">
              <span class="chat-sender">${isUser ? "Sir" : "J.A.R.V.I.S."}</span>
              <span class="chat-time">${msg.timestamp || ""}</span>
            </div>
            <div class="chat-bubble-body markdown-body">${formattedContent}</div>
            ${actionBadge}
            ${
              !isUser && (latencyBadge || modelBadge)
                ? `<div class="chat-bubble-footer">${modelBadge}${latencyBadge}</div>`
                : ""
            }
          </div>
        </div>
      `;
    })
    .join("");

  // Scroll to bottom
  container.scrollTop = container.scrollHeight;
}

/**
 * Rich Markdown formatter:
 * - Code fences with copy, download, and live preview buttons
 * - Markdown tables
 * - Bold, italic, headers, bullet points, and links
 */
export function formatChatMarkdown(text: string, getCodeId: () => number): string {
  if (!text) return "";

  // 1. Extract and format code blocks
  let formatted = text.replace(/```([a-zA-Z0-9_\-\.]*)\n([\s\S]*?)```/g, (_, lang, code) => {
    const cid = `code-block-${getCodeId()}`;
    const cleanLang = (lang || "code").toLowerCase();
    const canPreview = ["html", "css", "javascript", "js", "svg"].includes(cleanLang) || code.includes("<html") || code.includes("<div");

    const previewBtn = canPreview
      ? `<button class="code-action-btn" data-action="preview" data-target="${cid}">▶ Live Preview</button>`
      : "";

    return `
      <div class="code-artifact-card">
        <div class="code-artifact-header">
          <span class="code-artifact-lang">${escapeHtml(cleanLang.toUpperCase() || "CODE")}</span>
          <div class="code-artifact-actions">
            ${previewBtn}
            <button class="code-action-btn" data-action="copy" data-target="${cid}">Copy</button>
            <button class="code-action-btn" data-action="download" data-target="${cid}" data-lang="${cleanLang}">Download</button>
          </div>
        </div>
        <pre><code id="${cid}" class="language-${cleanLang}">${escapeHtml(code.trim())}</code></pre>
      </div>
    `;
  });

  // 2. Parse Markdown Tables
  formatted = formatted.replace(/(?:(?:^|\n)\|[^\n]+\|\r?\n(?:\|[ :-]+\|\r?\n)?(?:\|[^\n]+\|\r?\n?)+)/g, (tableMatch) => {
    const rows = tableMatch.trim().split("\n").map(r => r.trim()).filter(Boolean);
    if (rows.length < 2) return tableMatch;

    let html = `<div class="chat-table-wrapper"><table class="chat-table">`;
    const headerCols = rows[0].split("|").slice(1, -1).map(c => c.trim());
    html += `<thead><tr>${headerCols.map(c => `<th>${formatInlineMarkdown(c)}</th>`).join("")}</tr></thead><tbody>`;

    const startIdx = rows[1].includes("---") || rows[1].includes(":-") ? 2 : 1;
    for (let i = startIdx; i < rows.length; i++) {
      const cols = rows[i].split("|").slice(1, -1).map(c => c.trim());
      html += `<tr>${cols.map(c => `<td>${formatInlineMarkdown(c)}</td>`).join("")}</tr>`;
    }
    html += `</tbody></table></div>`;
    return html;
  });

  // 3. Headers
  formatted = formatted.replace(/^### (.*$)/gim, '<h4>$1</h4>');
  formatted = formatted.replace(/^## (.*$)/gim, '<h3>$1</h3>');
  formatted = formatted.replace(/^# (.*$)/gim, '<h2>$1</h2>');

  // 4. Bullet lists
  formatted = formatted.replace(/^\s*[\-\*]\s+(.*$)/gim, '<li>$1</li>');
  formatted = formatted.replace(/(<li>.*<\/li>)/gim, '<ul>$1</ul>');
  formatted = formatted.replace(/<\/ul>\s*<ul>/gim, '');

  // 5. Inline formatting (bold, italic, code, links)
  formatted = formatInlineMarkdown(formatted);

  // 6. Paragraphs / linebreaks
  formatted = formatted.replace(/\n\n/g, '<br/><br/>');

  return formatted;
}

function formatInlineMarkdown(text: string): string {
  let res = text;
  res = res.replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>');
  res = res.replace(/\*(.*?)\*/g, '<em>$1</em>');
  res = res.replace(/`([^`]+)`/g, '<code class="chat-inline-code">$1</code>');
  res = res.replace(/\[([^\]]+)\]\((https?:\/\/[^\)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  return res;
}

export function openPreviewModal(code: string) {
  const modal = document.getElementById("chat-preview-modal");
  const iframe = document.getElementById("chat-preview-iframe") as HTMLIFrameElement;
  if (modal && iframe) {
    iframe.srcdoc = code.includes("<html") ? code : `
      <!DOCTYPE html>
      <html>
      <head>
        <meta charset="utf-8">
        <style>
          body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; padding: 20px; background: #0f172a; color: #f8fafc; margin: 0; }
        </style>
      </head>
      <body>${code}</body>
      </html>
    `;
    modal.classList.add("open");
  }
}

export function closePreviewModal() {
  const modal = document.getElementById("chat-preview-modal");
  if (modal) modal.classList.remove("open");
}

export function downloadCodeFile(code: string, lang: string) {
  const ext = lang === "html" ? "html" : lang === "css" ? "css" : lang === "javascript" || lang === "js" ? "js" : lang === "python" || lang === "py" ? "py" : "txt";
  const blob = new Blob([code], { type: "text/plain;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `fixo-artifact-${Date.now()}.${ext}`;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

export async function sendTypedMessage(text: string) {
  try {
    const res = await fetch("/api/chat/send", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    });
    if (res.ok) {
      const data = await res.json();
      if (data.user_message) addChatMessage(data.user_message);
      if (data.fixo_message) addChatMessage(data.fixo_message);
      else if (data.jarvis_message) addChatMessage(data.jarvis_message);
    }
  } catch (err) {
    console.error("Failed to send typed message:", err);
  }
}

export function toggleChatPanel() {
  initChatPanel();
  isOpen = !isOpen;

  if (chatContainer) {
    if (isOpen) {
      chatContainer.classList.add("open");
      fetchChatHistory(currentSessionId || undefined);
      fetchSessionsList();
      fetchGraphStats();
      setTimeout(() => {
        const input = document.getElementById("chat-input-box") as HTMLInputElement;
        input?.focus();
      }, 300);
    } else {
      chatContainer.classList.remove("open");
    }
  }
}

function escapeHtml(text: string): string {
  const div = document.createElement("div");
  div.textContent = text;
  return div.innerHTML;
}
