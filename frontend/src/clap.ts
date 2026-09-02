/**
 * Real-time Acoustic Clap Detector for JARVIS.
 * 
 * Uses Web Audio API to detect sharp acoustic transient peaks (hand claps)
 * and triggers a callback when 3 distinct claps occur within a 1.8-second window.
 */

export interface ClapDetector {
  start(): Promise<void>;
  stop(): void;
  isActive(): boolean;
}

export function createClapDetector(
  audioContext: AudioContext,
  onTripleClap: () => void,
  onSingleClap?: (count: number) => void
): ClapDetector {
  let stream: MediaStream | null = null;
  let sourceNode: MediaStreamAudioSourceNode | null = null;
  let analyser: AnalyserNode | null = null;
  let scriptNode: ScriptProcessorNode | null = null;
  let running = false;

  const clapTimestamps: number[] = [];
  let lastClapTime = 0;
  const MIN_CLAP_INTERVAL_MS = 140; // Minimum time between claps (prevents echoes)
  const MAX_TRIPLE_CLAP_WINDOW_MS = 1800; // Maximum window for 3 claps
  const ENERGY_THRESHOLD = 0.28; // Normalized energy threshold for clap detection

  // Moving average baseline energy
  let baselineEnergy = 0.02;

  async function start() {
    if (running) return;

    try {
      if (audioContext.state === "suspended") {
        await audioContext.resume();
      }

      stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: false,
          noiseSuppression: false,
          autoGainControl: false,
        },
      });

      sourceNode = audioContext.createMediaStreamSource(stream);
      analyser = audioContext.createAnalyser();
      analyser.fftSize = 512;
      analyser.smoothingTimeConstant = 0.1;

      // Use ScriptProcessorNode for real-time buffer analysis
      scriptNode = audioContext.createScriptProcessor(512, 1, 1);

      scriptNode.onaudioprocess = (e) => {
        if (!running) return;
        const inputData = e.inputBuffer.getChannelData(0);

        let sumSquares = 0;
        let peak = 0;

        for (let i = 0; i < inputData.length; i++) {
          const val = Math.abs(inputData[i]);
          if (val > peak) peak = val;
          sumSquares += val * val;
        }

        const rms = Math.sqrt(sumSquares / inputData.length);
        baselineEnergy = baselineEnergy * 0.95 + rms * 0.05;

        // A clap is characterized by a high peak-to-average ratio and instantaneous rise
        const now = performance.now();
        const isTransient = peak > ENERGY_THRESHOLD && peak > baselineEnergy * 4.0;

        if (isTransient && now - lastClapTime > MIN_CLAP_INTERVAL_MS) {
          lastClapTime = now;
          clapTimestamps.push(now);

          // Purge claps older than MAX_TRIPLE_CLAP_WINDOW_MS
          while (
            clapTimestamps.length > 0 &&
            now - clapTimestamps[0] > MAX_TRIPLE_CLAP_WINDOW_MS
          ) {
            clapTimestamps.shift();
          }

          const count = clapTimestamps.length;
          console.log(`[clap] Clap detected! (#${count} in window)`);
          onSingleClap?.(count);

          if (count >= 3) {
            console.log("[clap] Triple clap detected! Waking up JARVIS...");
            clapTimestamps.length = 0; // Reset
            onTripleClap();
          }
        }
      };

      sourceNode.connect(analyser);
      analyser.connect(scriptNode);
      // Connect to a silent gain to prevent feedback loop to speakers
      const silentGain = audioContext.createGain();
      silentGain.gain.value = 0;
      scriptNode.connect(silentGain);
      silentGain.connect(audioContext.destination);

      running = true;
      console.log("[clap] Clap detector active");
    } catch (err) {
      console.warn("[clap] Could not initialize clap detector:", err);
    }
  }

  function stop() {
    running = false;
    if (scriptNode) {
      scriptNode.disconnect();
      scriptNode = null;
    }
    if (analyser) {
      analyser.disconnect();
      analyser = null;
    }
    if (sourceNode) {
      sourceNode.disconnect();
      sourceNode = null;
    }
    if (stream) {
      stream.getTracks().forEach((track) => track.stop());
      stream = null;
    }
    clapTimestamps.length = 0;
    console.log("[clap] Clap detector stopped");
  }

  return {
    start,
    stop,
    isActive: () => running,
  };
}
