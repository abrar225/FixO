/**
 * JARVIS — Settings Panel
 *
 * Overlay panel for provider keys, coding engine status, preferences, and
 * system info.
 */

interface StatusResponse {
  coding_engines: {
    opencode: boolean;
    ollama: boolean;
  };
  calendar_accessible: boolean;
  mail_accessible: boolean;
  notes_accessible: boolean;
  memory_count: number;
  task_count: number;
  server_port: number;
  uptime_seconds: number;
  env_keys_set: {
    groq: boolean;
    gemini: boolean;
    nvidia: boolean;
    firecrawl?: boolean;
    ollama?: boolean;
    use_local_brain?: boolean;
    user_name: string;
  };
}

interface PreferencesResponse {
  user_name: string;
  honorific: string;
  calendar_accounts: string;
}

interface ProviderTestResponse {
  valid: boolean;
  error?: string;
}

let panelEl: HTMLElement | null = null;
let isOpen = false;
let isFirstTimeSetup = false;
let setupStep = 0; // 0=brains, 1=voice, 2=name, 3=done

async function apiGet<T>(url: string): Promise<T> {
  const res = await fetch(url);
  return res.json();
}

async function apiPost<T>(url: string, body: unknown): Promise<T> {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return res.json();
}

function buildPanelHTML(): string {
  return `
    <div class="settings-backdrop" id="settings-backdrop"></div>
    <div class="settings-panel" id="settings-panel-inner">
      <div class="settings-header">
        <h2>Settings</h2>
        <button class="settings-close" id="settings-close">&times;</button>
      </div>

      <div class="settings-welcome" id="settings-welcome" style="display:none">
        <p>Welcome to JARVIS. Let's wire up the hybrid brain.</p>
      </div>

      <div class="settings-body">
        <section class="settings-section" id="section-api-keys">
          <h3>Provider Keys</h3>

          <div class="settings-field">
            <label>Groq API Key</label>
            <div class="settings-input-row">
              <input type="password" id="input-groq-key" placeholder="Groq key..." />
              <button class="settings-btn" id="btn-test-groq">Test</button>
              <span class="status-dot" id="status-groq"></span>
            </div>
          </div>

          <div class="settings-field">
            <label>Gemini API Key</label>
            <div class="settings-input-row">
              <input type="password" id="input-gemini-key" placeholder="Gemini key..." />
              <button class="settings-btn" id="btn-test-gemini">Test</button>
              <span class="status-dot" id="status-gemini"></span>
            </div>
          </div>

          <div class="settings-field">
            <label>NVIDIA API Key (Optional)</label>
            <div class="settings-input-row">
              <input type="password" id="input-nvidia-key" placeholder="NVIDIA key..." />
              <button class="settings-btn" id="btn-test-nvidia">Test</button>
              <span class="status-dot" id="status-nvidia"></span>
            </div>
          </div>

          <div class="settings-field">
            <label>Firecrawl API Key (Web Scraping - Optional)</label>
            <div class="settings-input-row">
              <input type="password" id="input-firecrawl-key" placeholder="Firecrawl key (fc-...)" />
              <button class="settings-btn" id="btn-test-firecrawl">Test</button>
              <span class="status-dot" id="status-firecrawl"></span>
            </div>
          </div>

          <div class="settings-actions">
            <button class="settings-btn primary" id="btn-save-keys">Save Keys</button>
          </div>
        </section>

        <section class="settings-section" id="section-status">
          <h3>Connection Status</h3>
          <div class="status-grid">
            <div class="status-row"><span class="status-dot" id="status-opencode"></span><span>OpenCode</span></div>
            <div class="status-row"><span class="status-dot" id="status-ollama"></span><span>Ollama</span></div>
            <div class="status-row"><span class="status-dot" id="status-calendar"></span><span>Apple Calendar</span></div>
            <div class="status-row"><span class="status-dot" id="status-mail"></span><span>Apple Mail</span></div>
            <div class="status-row"><span class="status-dot" id="status-notes"></span><span>Apple Notes</span></div>
            <div class="status-row"><span class="status-dot" id="status-server"></span><span>Server</span><span class="status-detail" id="status-server-detail"></span></div>
          </div>
        </section>

        <section class="settings-section" id="section-preferences">
          <h3>User Preferences</h3>

          <div class="settings-field">
            <label>Your Name</label>
            <input type="text" id="input-user-name" placeholder="Your name" />
          </div>

          <div class="settings-field">
            <label>Honorific</label>
            <select id="input-honorific">
              <option value="sir">Sir</option>
              <option value="ma'am">Ma'am</option>
              <option value="none">None</option>
            </select>
          </div>

          <div class="settings-field">
            <label>Calendar Accounts</label>
            <textarea id="input-calendar-accounts" rows="2" placeholder="auto (or comma-separated emails)"></textarea>
          </div>

          <div class="settings-actions">
            <button class="settings-btn primary" id="btn-save-prefs">Save Preferences</button>
          </div>
        </section>

        <section class="settings-section" id="section-sysinfo">
          <h3>System Info</h3>
          <div class="sysinfo-grid">
            <div class="sysinfo-row"><span class="sysinfo-label">Memory entries</span><span id="sysinfo-memory">--</span></div>
            <div class="sysinfo-row"><span class="sysinfo-label">Tasks</span><span id="sysinfo-tasks">--</span></div>
            <div class="sysinfo-row"><span class="sysinfo-label">Server port</span><span id="sysinfo-port">--</span></div>
            <div class="sysinfo-row"><span class="sysinfo-label">Uptime</span><span id="sysinfo-uptime">--</span></div>
          </div>
        </section>

        <div class="setup-nav" id="setup-nav" style="display:none">
          <button class="settings-btn primary" id="btn-setup-next">Next</button>
        </div>
      </div>
    </div>
  `;
}

function createPanel(): HTMLElement {
  const container = document.createElement("div");
  container.id = "settings-container";
  container.innerHTML = buildPanelHTML();
  document.body.appendChild(container);
  return container;
}

function setDotStatus(id: string, status: "green" | "red" | "yellow" | "off") {
  const dot = document.getElementById(id);
  if (!dot) return;
  dot.className = "status-dot";
  if (status !== "off") dot.classList.add(`status-${status}`);
}

function formatUptime(seconds: number): string {
  if (seconds < 60) return `${Math.floor(seconds)}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  return `${h}h ${m}m`;
}

function providerKeyName(provider: string): string {
  switch (provider) {
    case "groq":
      return "GROQ_API_KEY";
    case "gemini":
      return "GEMINI_API_KEY";
    case "nvidia":
      return "NVIDIA_API_KEY";
    case "firecrawl":
      return "FIRECRAWL_API_KEY";
    default:
      throw new Error(`Unsupported provider: ${provider}`);
  }
}

function providerInputId(provider: string): string {
  return `input-${provider}-key`;
}

async function testProvider(provider: "groq" | "gemini" | "nvidia" | "firecrawl", statusId: string) {
  const input = document.getElementById(providerInputId(provider)) as HTMLInputElement | null;
  const key = input?.value.trim() || "";
  setDotStatus(statusId, "yellow");
  try {
    const result = await apiPost<ProviderTestResponse>("/api/settings/test-provider", {
      provider,
      key_value: key || undefined,
    });
    setDotStatus(statusId, result.valid ? "green" : "red");
  } catch {
    setDotStatus(statusId, "red");
  }
}

async function loadStatus() {
  try {
    const status = await apiGet<StatusResponse>("/api/settings/status");

    setDotStatus("status-opencode", status.coding_engines.opencode ? "green" : "red");
    setDotStatus("status-ollama", status.coding_engines.ollama ? "green" : "red");
    setDotStatus("status-calendar", status.calendar_accessible ? "green" : "red");
    setDotStatus("status-mail", status.mail_accessible ? "green" : "red");
    setDotStatus("status-notes", status.notes_accessible ? "green" : "red");
    setDotStatus("status-server", "green");

    const serverDetail = document.getElementById("status-server-detail");
    if (serverDetail) serverDetail.textContent = `port ${status.server_port} | up ${formatUptime(status.uptime_seconds)}`;

    setDotStatus("status-groq", status.env_keys_set.groq ? "green" : "red");
    setDotStatus("status-gemini", status.env_keys_set.gemini ? "green" : "red");
    setDotStatus("status-nvidia", status.env_keys_set.nvidia ? "green" : "off");
    setDotStatus("status-firecrawl", status.env_keys_set.firecrawl ? "green" : "off");

    const memEl = document.getElementById("sysinfo-memory");
    if (memEl) memEl.textContent = String(status.memory_count);
    const taskEl = document.getElementById("sysinfo-tasks");
    if (taskEl) taskEl.textContent = String(status.task_count);
    const portEl = document.getElementById("sysinfo-port");
    if (portEl) portEl.textContent = String(status.server_port);
    const upEl = document.getElementById("sysinfo-uptime");
    if (upEl) upEl.textContent = formatUptime(status.uptime_seconds);

    return status;
  } catch (e) {
    console.error("[settings] failed to load status:", e);
    setDotStatus("status-server", "red");
    return null;
  }
}

async function loadPreferences() {
  try {
    const prefs = await apiGet<PreferencesResponse>("/api/settings/preferences");
    const nameEl = document.getElementById("input-user-name") as HTMLInputElement | null;
    const honEl = document.getElementById("input-honorific") as HTMLSelectElement | null;
    const calEl = document.getElementById("input-calendar-accounts") as HTMLTextAreaElement | null;
    if (nameEl) nameEl.value = prefs.user_name || "";
    if (honEl) honEl.value = prefs.honorific || "sir";
    if (calEl) calEl.value = prefs.calendar_accounts || "auto";
  } catch (e) {
    console.error("[settings] failed to load preferences:", e);
  }
}

function wireEvents() {
  document.getElementById("settings-close")?.addEventListener("click", closeSettings);
  document.getElementById("settings-backdrop")?.addEventListener("click", closeSettings);

  document.getElementById("btn-save-keys")?.addEventListener("click", async () => {
    const providers = ["groq", "gemini", "nvidia", "firecrawl"] as const;
    for (const provider of providers) {
      const input = document.getElementById(providerInputId(provider)) as HTMLInputElement | null;
      const value = input?.value.trim();
      if (value) {
        await apiPost("/api/settings/keys", { key_name: providerKeyName(provider), key_value: value });
      }
    }
    await loadStatus();
  });

  document.getElementById("btn-test-groq")?.addEventListener("click", async () => testProvider("groq", "status-groq"));
  document.getElementById("btn-test-gemini")?.addEventListener("click", async () => testProvider("gemini", "status-gemini"));
  document.getElementById("btn-test-nvidia")?.addEventListener("click", async () => testProvider("nvidia", "status-nvidia"));
  document.getElementById("btn-test-firecrawl")?.addEventListener("click", async () => testProvider("firecrawl", "status-firecrawl"));

  document.getElementById("btn-save-prefs")?.addEventListener("click", async () => {
    const user_name = (document.getElementById("input-user-name") as HTMLInputElement | null)?.value.trim() || "";
    const honorific = (document.getElementById("input-honorific") as HTMLSelectElement | null)?.value || "sir";
    const calendar_accounts = (document.getElementById("input-calendar-accounts") as HTMLTextAreaElement | null)?.value.trim() || "auto";
    await apiPost("/api/settings/preferences", { user_name, honorific, calendar_accounts });
    await loadStatus();
  });

  document.getElementById("btn-setup-next")?.addEventListener("click", advanceSetup);
}

function enterSetupMode() {
  isFirstTimeSetup = true;
  setupStep = 0;

  const welcome = document.getElementById("settings-welcome");
  if (welcome) welcome.style.display = "block";

  const nav = document.getElementById("setup-nav");
  if (nav) nav.style.display = "flex";

  showSetupStep(0);
}

function showSetupStep(step: number) {
  const sections = ["section-api-keys", "section-status", "section-preferences", "section-sysinfo"];
  sections.forEach((id, i) => {
    const el = document.getElementById(id);
    if (!el) return;
    if (step === 0 && i === 0) el.style.display = "";
    else if (step === 1 && i === 0) el.style.display = "";
    else if (step === 2 && i === 2) el.style.display = "";
    else if (step === 3) el.style.display = "";
    else el.style.display = "none";
  });

  const nextBtn = document.getElementById("btn-setup-next");
  if (!nextBtn) return;
  if (step === 0) nextBtn.textContent = "Next: Voice";
  else if (step === 1) nextBtn.textContent = "Next: Your Name";
  else if (step === 2) nextBtn.textContent = "Finish Setup";
  else nextBtn.style.display = "none";
}

async function advanceSetup() {
  setupStep++;
  if (setupStep >= 3) {
    isFirstTimeSetup = false;
    const welcome = document.getElementById("settings-welcome");
    if (welcome) welcome.style.display = "none";
    const nav = document.getElementById("setup-nav");
    if (nav) nav.style.display = "none";
    ["section-api-keys", "section-status", "section-preferences", "section-sysinfo"].forEach((id) => {
      const el = document.getElementById(id);
      if (el) el.style.display = "";
    });
    closeSettings();
    return;
  }
  showSetupStep(setupStep);
}

export async function openSettings() {
  if (isOpen) return;
  isOpen = true;

  if (!panelEl) {
    panelEl = createPanel();
    wireEvents();
  }

  panelEl.style.display = "block";
  requestAnimationFrame(() => {
    panelEl!.classList.add("open");
  });

  const status = await loadStatus();
  await loadPreferences();

  if (status && !status.env_keys_set.use_local_brain && (!status.env_keys_set.groq || !status.env_keys_set.gemini)) {
    enterSetupMode();
  }
}

export function closeSettings() {
  if (!panelEl || !isOpen) return;
  isOpen = false;
  panelEl.classList.remove("open");
  setTimeout(() => {
    if (panelEl) panelEl.style.display = "none";
  }, 300);
}

export function isSettingsOpen(): boolean {
  return isOpen;
}

export async function checkFirstTimeSetup(): Promise<boolean> {
  try {
    const status = await apiGet<StatusResponse>("/api/settings/status");
    if (!status.env_keys_set.use_local_brain && (!status.env_keys_set.groq || !status.env_keys_set.gemini)) {
      openSettings();
      return true;
    }
  } catch {
    // Server not ready yet, skip.
  }
  return false;
}
