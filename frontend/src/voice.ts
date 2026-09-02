/**
 * Voice input (Web Speech API) and audio output (AudioContext) for JARVIS.
 */

// ---------------------------------------------------------------------------
// Speech Recognition
// ---------------------------------------------------------------------------

export interface VoiceInput {
  start(): void;
  stop(): void;
  pause(): void;
  resume(): void;
  setSleeping(sleeping: boolean): void;
  isSleeping(): boolean;
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
declare const webkitSpeechRecognition: any;

const WAKE_PHRASES = [
  "hey jarvis",
  "jarvis",
  "listen jarvis",
  "wake up",
  "wake up jarvis",
  "start listening",
  "are you there jarvis",
];

const SLEEP_PHRASES = [
  "shut up",
  "shut the fuck up",
  "stop listening",
  "go to sleep",
  "be quiet",
  "turn off mic",
  "turn off the mic",
  "turn your mic off",
  "stay idle",
  "mute",
  "sleep",
];

export function createVoiceInput(
  onTranscript: (text: string) => void,
  onError: (msg: string) => void,
  callbacks?: {
    onWakeWord?: (wakePhrase: string) => void;
    onSleepCommand?: (sleepPhrase: string) => void;
  }
): VoiceInput {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const SR = (window as any).SpeechRecognition || (typeof webkitSpeechRecognition !== "undefined" ? webkitSpeechRecognition : null);
  if (!SR) {
    onError("Speech recognition not supported in this browser");
    return {
      start() {},
      stop() {},
      pause() {},
      resume() {},
      setSleeping() {},
      isSleeping: () => false,
    };
  }

  const recognition = new SR();
  recognition.continuous = true;
  recognition.interimResults = true;
  recognition.lang = "en-US";

  let shouldListen = false;
  let paused = false;
  let sleeping = false;

  function matchesWakeWord(text: string): string | null {
    const t = text.toLowerCase().trim();
    for (const phrase of WAKE_PHRASES) {
      if (t === phrase || t.startsWith(phrase + " ") || t.endsWith(" " + phrase) || t.includes(" " + phrase + " ")) {
        return phrase;
      }
    }
    return null;
  }

  function matchesSleepCommand(text: string): string | null {
    const t = text.toLowerCase().trim();
    for (const phrase of SLEEP_PHRASES) {
      if (t === phrase || t.startsWith(phrase + " ") || t.endsWith(" " + phrase) || t.includes(" " + phrase + " ")) {
        return phrase;
      }
    }
    return null;
  }

  recognition.onresult = (event: any) => {
    for (let i = event.resultIndex; i < event.results.length; i++) {
      const isFinal = Boolean(event.results[i].isFinal);
      const text = event.results[i][0].transcript.trim();
      if (!text) continue;

      if (sleeping) {
        // In sleep mode: only wake up on final wake word match
        if (isFinal) {
          const wakeMatch = matchesWakeWord(text);
          if (wakeMatch) {
            console.log(`[voice] Wake word detected: "${wakeMatch}"`);
            sleeping = false;
            callbacks?.onWakeWord?.(wakeMatch);
          }
        }
      } else {
        // In active mode: check for sleep commands on final transcript
        if (isFinal) {
          const sleepMatch = matchesSleepCommand(text);
          if (sleepMatch) {
            console.log(`[voice] Sleep command detected: "${sleepMatch}"`);
            sleeping = true;
            callbacks?.onSleepCommand?.(sleepMatch);
            return;
          }
          onTranscript(text);
        }
      }
    }
  };

  recognition.onend = () => {
    if (shouldListen && !paused) {
      try {
        recognition.start();
      } catch {
        // Already started
      }
    }
  };

  recognition.onerror = (event: any) => {
    if (event.error === "not-allowed") {
      onError("Microphone access denied. Please allow microphone access.");
      shouldListen = false;
    } else if (event.error === "no-speech") {
      // Normal, just restart
    } else if (event.error === "aborted") {
      // Expected during pause
    } else {
      console.warn("[voice] recognition error:", event.error);
    }
  };

  return {
    start() {
      shouldListen = true;
      paused = false;
      try {
        recognition.start();
      } catch {
        // Already started
      }
    },
    stop() {
      shouldListen = false;
      paused = false;
      recognition.stop();
    },
    pause() {
      paused = true;
      recognition.stop();
    },
    resume() {
      paused = false;
      if (shouldListen) {
        try {
          recognition.start();
        } catch {
          // Already started
        }
      }
    },
    setSleeping(val: boolean) {
      sleeping = val;
    },
    isSleeping: () => sleeping,
  };
}

// ---------------------------------------------------------------------------
// Audio Player
// ---------------------------------------------------------------------------

export interface AudioPlayer {
  enqueue(base64: string): Promise<void>;
  stop(): void;
  getAnalyser(): AnalyserNode;
  onFinished(cb: () => void): void;
}

export function createAudioPlayer(): AudioPlayer {
  const audioCtx = new AudioContext();
  const analyser = audioCtx.createAnalyser();
  analyser.fftSize = 256;
  analyser.smoothingTimeConstant = 0.8;
  analyser.connect(audioCtx.destination);

  const queue: AudioBuffer[] = [];
  let isPlaying = false;
  let currentSource: AudioBufferSourceNode | null = null;
  let finishedCallback: (() => void) | null = null;

  function playNext() {
    if (queue.length === 0) {
      isPlaying = false;
      currentSource = null;
      finishedCallback?.();
      return;
    }

    isPlaying = true;
    const buffer = queue.shift()!;
    const source = audioCtx.createBufferSource();
    source.buffer = buffer;
    source.connect(analyser);
    currentSource = source;

    source.onended = () => {
      if (currentSource === source) {
        playNext();
      }
    };

    source.start();
  }

  return {
    async enqueue(base64: string) {
      // Resume audio context (browser autoplay policy)
      if (audioCtx.state === "suspended") {
        await audioCtx.resume();
      }

      try {
        const binary = atob(base64);
        const bytes = new Uint8Array(binary.length);
        for (let i = 0; i < binary.length; i++) {
          bytes[i] = binary.charCodeAt(i);
        }
        const audioBuffer = await audioCtx.decodeAudioData(bytes.buffer.slice(0));
        queue.push(audioBuffer);
        if (!isPlaying) playNext();
      } catch (err) {
        console.error("[audio] decode error:", err);
        // Skip bad audio, continue
        if (!isPlaying && queue.length > 0) playNext();
      }
    },

    stop() {
      queue.length = 0;
      if (currentSource) {
        try {
          currentSource.stop();
        } catch {
          // Already stopped
        }
        currentSource = null;
      }
      isPlaying = false;
    },

    getAnalyser() {
      return analyser;
    },

    onFinished(cb: () => void) {
      finishedCallback = cb;
    },
  };
}
