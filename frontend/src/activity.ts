/**
 * JARVIS Activity & Resource Manager
 *
 * Real-time telemetry: CPU, RAM, Apple M4 GPU / Neural Engine,
 * latency statistics, active background tasks, and live event log.
 */

let activityContainer: HTMLElement | null = null;
let pollInterval: number | null = null;
let isOpen = false;

export function initActivityManager() {
  if (document.getElementById("activity-container")) return;

  activityContainer = document.createElement("div");
  activityContainer.id = "activity-container";
  activityContainer.innerHTML = `
    <div class="hud-backdrop" id="activity-backdrop"></div>
    <div class="hud-panel">
      <div class="hud-header">
        <div class="hud-title-group">
          <div class="hud-pulse-dot"></div>
          <h2>System Activity & Resource Telemetry</h2>
          <span class="hud-chip-badge" id="hud-chip-name">Apple Silicon</span>
        </div>
        <button class="hud-close" id="activity-close" title="Close Telemetry">✕</button>
      </div>

      <div class="hud-grid">
        <!-- CPU Card -->
        <div class="hud-card">
          <div class="hud-card-header">
            <span class="hud-card-label">CPU Utilization</span>
            <span class="hud-card-val" id="cpu-percent">0%</span>
          </div>
          <div class="hud-bar-container">
            <div class="hud-bar" id="cpu-bar" style="width: 0%"></div>
          </div>
          <div class="hud-card-meta">
            <span id="cpu-cores">10 Cores</span>
            <span id="cpu-load">Load: 0.00, 0.00, 0.00</span>
          </div>
        </div>

        <!-- RAM Card -->
        <div class="hud-card">
          <div class="hud-card-header">
            <span class="hud-card-label">Unified RAM</span>
            <span class="hud-card-val" id="ram-percent">0%</span>
          </div>
          <div class="hud-bar-container">
            <div class="hud-bar hud-bar-purple" id="ram-bar" style="width: 0%"></div>
          </div>
          <div class="hud-card-meta">
            <span id="ram-usage">0.0 GB / 0.0 GB</span>
            <span id="ram-status">Unified Memory</span>
          </div>
        </div>

        <!-- GPU & Neural Engine Card -->
        <div class="hud-card">
          <div class="hud-card-header">
            <span class="hud-card-label">GPU & Neural Engine</span>
            <span class="hud-status-badge status-active">Active</span>
          </div>
          <div class="hud-gpu-info">
            <div class="hud-spec-row">
              <span>Graphics Architecture</span>
              <strong id="gpu-name">Apple M4 GPU</strong>
            </div>
            <div class="hud-spec-row">
              <span>Apple Neural Engine</span>
              <strong>16-Core Matrix Engine</strong>
            </div>
          </div>
        </div>

        <!-- Core Brain Latency -->
        <div class="hud-card">
          <div class="hud-card-header">
            <span class="hud-card-label">Brain & Uptime</span>
            <span class="hud-card-val" id="server-uptime">0s</span>
          </div>
          <div class="hud-gpu-info">
            <div class="hud-spec-row">
              <span>Fast Brain</span>
              <strong class="highlight-cyan">Gemma 2B (Local)</strong>
            </div>
            <div class="hud-spec-row">
              <span>Think Brain</span>
              <strong class="highlight-blue">Qwen Think (Local)</strong>
            </div>
          </div>
        </div>
      </div>

      <!-- Live Activity Stream -->
      <div class="hud-stream-section">
        <div class="hud-stream-header">
          <h3>Real-Time Activity Log</h3>
          <span class="hud-live-tag">● LIVE STREAM</span>
        </div>
        <div class="hud-log-feed" id="hud-log-feed">
          <div class="hud-log-empty">Telemetry stream initializing...</div>
        </div>
      </div>
    </div>
  `;

  document.body.appendChild(activityContainer);

  document.getElementById("activity-close")?.addEventListener("click", toggleActivityManager);
  document.getElementById("activity-backdrop")?.addEventListener("click", toggleActivityManager);
}

export async function fetchActivityData() {
  try {
    const res = await fetch("/api/system/activity");
    if (!res.ok) return;
    const data = await res.json();

    const metrics = data.metrics || {};
    const activities = data.activities || [];

    // CPU
    const cpuPct = metrics.cpu?.percent ?? 0;
    const cpuEl = document.getElementById("cpu-percent");
    const cpuBar = document.getElementById("cpu-bar");
    const cpuCores = document.getElementById("cpu-cores");
    const cpuLoad = document.getElementById("cpu-load");

    if (cpuEl) cpuEl.textContent = `${cpuPct.toFixed(1)}%`;
    if (cpuBar) cpuBar.style.width = `${Math.min(100, Math.max(0, cpuPct))}%`;
    if (cpuCores && metrics.cpu?.cores) cpuCores.textContent = `${metrics.cpu.cores} Cores`;
    if (cpuLoad && metrics.cpu?.load) cpuLoad.textContent = `Load: ${metrics.cpu.load.join(", ")}`;

    // RAM
    const ramPct = metrics.ram?.percent ?? 0;
    const ramEl = document.getElementById("ram-percent");
    const ramBar = document.getElementById("ram-bar");
    const ramUsage = document.getElementById("ram-usage");

    if (ramEl) ramEl.textContent = `${ramPct.toFixed(1)}%`;
    if (ramBar) ramBar.style.width = `${Math.min(100, Math.max(0, ramPct))}%`;
    if (ramUsage && metrics.ram) ramUsage.textContent = `${metrics.ram.used_gb} GB / ${metrics.ram.total_gb} GB`;

    // Chip & GPU
    const chipBadge = document.getElementById("hud-chip-name");
    const gpuName = document.getElementById("gpu-name");
    if (chipBadge && metrics.chip) chipBadge.textContent = metrics.chip;
    if (gpuName && metrics.gpu?.name) gpuName.textContent = metrics.gpu.name;

    // Uptime
    const uptimeEl = document.getElementById("server-uptime");
    if (uptimeEl && metrics.uptime_seconds !== undefined) {
      const mins = Math.floor(metrics.uptime_seconds / 60);
      const secs = metrics.uptime_seconds % 60;
      uptimeEl.textContent = `${mins}m ${secs}s`;
    }

    // Activity log feed
    const feed = document.getElementById("hud-log-feed");
    if (feed) {
      if (activities.length === 0) {
        feed.innerHTML = `<div class="hud-log-empty">No activity events recorded yet, sir.</div>`;
      } else {
        feed.innerHTML = activities
          .map(
            (act: any) => `
          <div class="hud-log-item category-${act.category || "system"}">
            <span class="hud-log-time">${act.timestamp || ""}</span>
            <span class="hud-log-tag tag-${act.category || "system"}">${(act.category || "SYSTEM").toUpperCase()}</span>
            <div class="hud-log-content">
              <div class="hud-log-title">${escapeHtml(act.title || "")}</div>
              ${act.details ? `<div class="hud-log-sub">${escapeHtml(act.details)}</div>` : ""}
            </div>
            ${act.latency_ms ? `<span class="hud-log-lat">⚡ ${act.latency_ms}ms</span>` : ""}
          </div>
        `
          )
          .join("");
      }
    }
  } catch (err) {
    console.error("Failed to fetch activity telemetry:", err);
  }
}

export function toggleActivityManager() {
  initActivityManager();
  isOpen = !isOpen;

  if (activityContainer) {
    if (isOpen) {
      activityContainer.classList.add("open");
      fetchActivityData();
      if (!pollInterval) {
        pollInterval = window.setInterval(fetchActivityData, 1500);
      }
    } else {
      activityContainer.classList.remove("open");
      if (pollInterval) {
        clearInterval(pollInterval);
        pollInterval = null;
      }
    }
  }
}

function escapeHtml(text: string): string {
  const div = document.createElement("div");
  div.textContent = text;
  return div.innerHTML;
}
