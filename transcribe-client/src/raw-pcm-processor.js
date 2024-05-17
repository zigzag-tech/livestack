class RawPCMProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    this.maxInterval = options.processorOptions.maxInterval / 1000;
    this.audioBuffer = [];
    this.lastRecordedTime = currentTime;
  }

  process(inputs, outputs, parameters) {
    const input = inputs[0];

    if (input.length > 0) {
      const currentTime = currentFrame / sampleRate;
      const data = Int16Array.from(input[0], (n) => {
        const res = n < 0 ? n * 32768 : n * 32767; // convert in range [-32768, 32767]
        return Math.max(-32768, Math.min(32767, res)); // clamp
      });
      this.audioBuffer = Int16Array.from([...this.audioBuffer, ...data]);

      if (currentTime - this.lastRecordedTime >= this.maxInterval) {
        this.port.postMessage(this.audioBuffer);
        this.audioBuffer = [];
        this.lastRecordedTime = currentTime;
      }
    }

    return true;
  }
}

class VolumeProcessor extends AudioWorkletProcessor {
  process(inputs, outputs, parameters) {
    // post volume to main thread
    const input = inputs[0];

    // if (input.length === 0) return true;
    const volume = input.reduce((acc, channel) => {
      return acc + channel.reduce((acc, sample) => acc + Math.abs(sample), 0);
    }, 0);
    this.port.postMessage(volume);
    return true;
  }
}

registerProcessor("raw-pcm-processor", RawPCMProcessor);
registerProcessor("volume-processor", VolumeProcessor);
