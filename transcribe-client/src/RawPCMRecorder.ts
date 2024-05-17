export type AudioMessage =
  | { type: "data"; data: Int16Array }
  | { type: "volume"; volume: number };
export class RawPCMRecorder {
  private audioContext: AudioContext;
  private rawPCMWorkletNode: AudioWorkletNode | null = null;
  private volumeWorkletNode: AudioWorkletNode | null = null;
  private vadWorkletNode: AudioWorkletNode | null = null;
  private maxInterval: number;
  private listeners: ((message: AudioMessage) => void)[] = [];
  private stream: MediaStream | null = null;
  private lastSpeech = Date.now();
  private lastSilence = Date.now();

  constructor(maxInterval: number = 1000) {
    this.audioContext = new AudioContext({
      sampleRate: 16000,
    });
    this.maxInterval = maxInterval;
  }

  public async startRecording(): Promise<void> {
    const rawPCMProcessorUrl = new URL(
      "./raw-pcm-processor.js",
      import.meta.url
    );
    // const vadProcessorUrl = new URL(
    //   "/vad/vad-audio-worklet.js",
    //   import.meta.url
    // );
    // const fftUrl = new URL("./fft.js", import.meta.url);
    // console.log(fftUrl);

    await this.audioContext.audioWorklet.addModule(rawPCMProcessorUrl);
    await this.audioContext.audioWorklet.addModule("/vad/vad-audio-worklet.js");

    this.rawPCMWorkletNode = new AudioWorkletNode(
      this.audioContext,
      "raw-pcm-processor",
      {
        processorOptions: {
          maxInterval: this.maxInterval,
        },
      }
    );

    this.rawPCMWorkletNode.port.onmessage = (event) => {
      if (this.rawPCMWorkletNode) {
        // only send data if last speech is within 10 seconds or last silence is before last speech
        if (
          Date.now() - this.lastSpeech < 10000 ||
          this.lastSilence < this.lastSpeech
        ) {
          const data = event.data as Int16Array;
          this.listeners.forEach((listener) =>
            listener({
              type: "data",
              data,
            })
          );
        }
      }
    };

    this.volumeWorkletNode = new AudioWorkletNode(
      this.audioContext,
      "volume-processor"
    );

    this.volumeWorkletNode.port.onmessage = (event) => {
      const volume = event.data as number;
      this.listeners.forEach((listener) =>
        listener({
          type: "volume",
          volume,
        })
      );
    };
    this.stream = await navigator.mediaDevices.getUserMedia({
      audio: true,
      video: false,
    });
    const source = this.audioContext.createMediaStreamSource(this.stream);

    this.vadWorkletNode = new AudioWorkletNode(this.audioContext, "vad", {
      outputChannelCount: [1],
      processorOptions: {
        fftSize: 128,
        sampleRate: 16000,
      },
    });

    source.connect(this.rawPCMWorkletNode);
    source.connect(this.volumeWorkletNode);
    source.connect(this.vadWorkletNode);

    this.vadWorkletNode.port.onmessage = (event) => {
      if (event.data.cmd === "silence") {
        this.lastSilence = Date.now();
      } else if (event.data.cmd === "speech") {
        this.lastSpeech = Date.now();
      }
    };
    // this.rawPCMWorkletNode.connect(this.audioContext.destination);
    // this.volumeWorkletNode.connect(this.audioContext.destination);
    // this.vadWorkletNode.connect(this.audioContext.destination);
  }

  public stopRecording(): void {
    if (this.rawPCMWorkletNode) {
      this.rawPCMWorkletNode.disconnect();
      this.rawPCMWorkletNode = null;

      this.volumeWorkletNode?.disconnect();
      this.volumeWorkletNode = null;

      this.vadWorkletNode?.disconnect();
      this.vadWorkletNode = null;

      this.stream?.getTracks().forEach((track) => track.stop());
    }
  }

  public onData(listener: (data: AudioMessage) => void): void {
    this.listeners.push(listener);
  }

  public offData(listener: (data: AudioMessage) => void): void {
    const listenerIndex = this.listeners.indexOf(listener);
    if (listenerIndex !== -1) {
      this.listeners.splice(listenerIndex, 1);
    }
  }
}
