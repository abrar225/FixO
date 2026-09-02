/**
 * JARVIS — Main entry point.
 *
 * Wires together the orb visualization, WebSocket communication,
 * speech recognition, and audio playback into a single experience.
 */

import { createOrb, type OrbState } from "./orb";
import { createVoiceInput, createAudioPlayer } from "./voice";
import { createClapDetector } from "./clap";
import { createSocket } from "./ws";
import { openSettings, checkFirstTimeSetup } from "./settings";
import { initActivityManager, toggleActivityManager } from "./activity";
import { initChatPanel, toggleChatPanel, addChatMessage } from "./chat";
import "./style.css";

// ---------------------------------------------------------------------------
// State machine
// ---------------------------------------------------------------------------

type State = "idle" | "listening" | "thinking" | "speaking" | "sleeping";
let currentState: State = "idle";
let isMuted = false;

const statusEl = document.getElementById("status-text")!;
const errorEl = document.getElementById("error-text")!;

function showError(msg: string) {
  errorEl.textContent = msg;
  errorEl.style.opacity = "1";
  setTimeout(() => {
    errorEl.style.opacity = "0";
  }, 5000);
}

function updateStatus(state: State) {
  const labels: Record<State, string> = {
    idle: "",
    listening: "listening...",
    thinking: "thinking...",
    speaking: "",
    sleeping: "sleeping — say 'hey fixo' or clap 3x",
  };
  statusEl.textContent = labels[state];
}

// ---------------------------------------------------------------------------
// Init components
// ---------------------------------------------------------------------------

const canvas = document.getElementById("orb-canvas") as HTMLCanvasElement;
const orb = createOrb(canvas);

const wsProto = window.location.protocol === "https:" ? "wss:" : "ws:";
const WS_URL = `${wsProto}//${window.location.host}/ws/voice`;
const socket = createSocket(WS_URL);

const audioPlayer = createAudioPlayer();
const audioCtx = audioPlayer.getAnalyser().context as AudioContext;
orb.setAnalyser(audioPlayer.getAnalyser());

// Acoustic 3-Hand Clap Detector
const clapDetector = createClapDetector(
  audioCtx,
  () => {
    // 3 claps detected! Wake up FixO
    console.log("[main] Triple clap detected! Waking up FixO...");
    wakeUp("clap");
  },
  (count) => {
    if (currentState === "sleeping") {
      statusEl.textContent = `clap detected (${count}/3)...`;
      setTimeout(() => {
        if (currentState === "sleeping") {
          statusEl.textContent = "sleeping — say 'hey fixo' or clap 3x";
        }
      }, 1200);
    }
  }
);

// Initialize Chat and Activity HUD systems
initActivityManager();
initChatPanel((text: string) => {
  // When user sends typed message via Chat Panel
  if (currentState === "sleeping") {
    wakeUp("chat");
  }
  audioPlayer.stop();
  socket.send({ type: "transcript", text, isFinal: true });
  transition("thinking");
});

function transition(newState: State) {
  if (newState === currentState) return;
  currentState = newState;
  orb.setState(newState as OrbState);
  updateStatus(newState);

  switch (newState) {
    case "sleeping":
      voiceInput.setSleeping(true);
      clapDetector.start();
      break;
    case "idle":
      if (!isMuted) voiceInput.resume();
      break;
    case "listening":
      voiceInput.setSleeping(false);
      if (!isMuted) voiceInput.resume();
      break;
    case "thinking":
      voiceInput.pause();
      break;
    case "speaking":
      voiceInput.pause();
      break;
  }
}

function wakeUp(source: string = "voice", notifyServer: boolean = true) {
  if (currentState !== "sleeping" && !isMuted) return;
  console.log(`[main] Waking up JARVIS from ${source}`);
  isMuted = false;
  btnMute.classList.remove("muted");
  voiceInput.setSleeping(false);
  transition("listening");
  if (notifyServer) {
    socket.send({ type: "wake", source });
  }
}

function goToSleep(notifyServer: boolean = true) {
  if (currentState === "sleeping" && isMuted) return;
  console.log("[main] Putting JARVIS to sleep");
  audioPlayer.stop();
  voiceInput.setSleeping(true);
  isMuted = true;
  btnMute.classList.add("muted");
  transition("sleeping");
  if (notifyServer) {
    socket.send({ type: "sleep" });
  }
}

// ---------------------------------------------------------------------------
// Voice input
// ---------------------------------------------------------------------------

const voiceInput = createVoiceInput(
  (text: string) => {
    // Cancel any current JARVIS response before sending new input
    audioPlayer.stop();
    // User spoke — send transcript
    socket.send({ type: "transcript", text, isFinal: true });
    transition("thinking");
  },
  (msg: string) => {
    showError(msg);
  },
  {
    onWakeWord: (wakePhrase: string) => {
      console.log(`[main] Wake word: "${wakePhrase}"`);
      wakeUp("voice", true);
    },
    onSleepCommand: (sleepPhrase: string) => {
      console.log(`[main] Sleep command: "${sleepPhrase}"`);
      audioPlayer.stop();
      goToSleep(true);
    },
  }
);

// ---------------------------------------------------------------------------
// Audio playback finished
// ---------------------------------------------------------------------------

audioPlayer.onFinished(() => {
  if (currentState !== "sleeping") {
    transition("idle");
  }
});

// ---------------------------------------------------------------------------
// WebSocket messages
// ---------------------------------------------------------------------------

socket.onMessage((msg) => {
  const type = msg.type as string;

  if (type === "audio") {
    const audioData = msg.data as string;
    if (audioData) {
      if (currentState !== "speaking" && currentState !== "sleeping") {
        transition("speaking");
      }
      audioPlayer.enqueue(audioData);
    } else {
      if (currentState !== "sleeping") {
        transition("idle");
      }
    }
    if (msg.text) console.log("[JARVIS]", msg.text);
  } else if (type === "chat_event") {
    if (msg.message) {
      addChatMessage(msg.message as any);
    }
  } else if (type === "sleep") {
    goToSleep(false);
  } else if (type === "wake") {
    wakeUp("server", false);
  } else if (type === "status") {
    const state = msg.state as string;
    if (state === "thinking" && currentState !== "sleeping") {
      transition("thinking");
      if (msg.text) {
        statusEl.textContent = msg.text as string;
      }
    } else if (state === "working" && currentState !== "sleeping") {
      transition("thinking");
      statusEl.textContent = (msg.text as string) || "working...";
    } else if (state === "idle" && currentState !== "sleeping") {
      transition("idle");
    } else if (state === "sleeping") {
      goToSleep(false);
    }
  } else if (type === "text") {
    console.log("[JARVIS]", msg.text);
  } else if (type === "task_spawned") {
    console.log("[task]", "spawned:", msg.task_id, msg.prompt);
  } else if (type === "task_complete") {
    console.log("[task]", "complete:", msg.task_id, msg.status, msg.summary);
  }
});

// ---------------------------------------------------------------------------
// Kick off
// ---------------------------------------------------------------------------

setTimeout(() => {
  voiceInput.start();
  transition("listening");
}, 1000);

function ensureAudioContext() {
  const ctx = audioPlayer.getAnalyser().context as AudioContext;
  if (ctx.state === "suspended") {
    ctx.resume().then(() => console.log("[audio] context resumed"));
  }
}
document.addEventListener("click", ensureAudioContext);
document.addEventListener("touchstart", ensureAudioContext);
document.addEventListener("keydown", ensureAudioContext, { once: true });

ensureAudioContext();

// ---------------------------------------------------------------------------
// UI Controls
// ---------------------------------------------------------------------------

const btnChat = document.getElementById("btn-chat")!;
const btnActivity = document.getElementById("btn-activity")!;
const btnMute = document.getElementById("btn-mute")!;
const btnMenu = document.getElementById("btn-menu")!;
const menuDropdown = document.getElementById("menu-dropdown")!;
const btnRestart = document.getElementById("btn-restart")!;
const btnFixSelf = document.getElementById("btn-fix-self")!;

btnChat?.addEventListener("click", (e) => {
  e.stopPropagation();
  toggleChatPanel();
});

btnActivity?.addEventListener("click", (e) => {
  e.stopPropagation();
  toggleActivityManager();
});

btnMute.addEventListener("click", (e) => {
  e.stopPropagation();
  if (currentState === "sleeping") {
    wakeUp("button");
  } else {
    goToSleep();
  }
});

btnMenu.addEventListener("click", (e) => {
  e.stopPropagation();
  menuDropdown.style.display = menuDropdown.style.display === "none" ? "block" : "none";
});

document.addEventListener("click", () => {
  menuDropdown.style.display = "none";
});

btnRestart.addEventListener("click", async (e) => {
  e.stopPropagation();
  menuDropdown.style.display = "none";
  statusEl.textContent = "restarting...";
  try {
    await fetch("/api/restart", { method: "POST" });
    setTimeout(() => window.location.reload(), 4000);
  } catch {
    statusEl.textContent = "restart failed";
  }
});

btnFixSelf.addEventListener("click", (e) => {
  e.stopPropagation();
  menuDropdown.style.display = "none";
  socket.send({ type: "fix_self" });
  statusEl.textContent = "entering work mode...";
});

const btnSettings = document.getElementById("btn-settings")!;
btnSettings.addEventListener("click", (e) => {
  e.stopPropagation();
  menuDropdown.style.display = "none";
  openSettings();
});

setTimeout(() => {
  checkFirstTimeSetup();
}, 2000);
