import { RawPCMRecorder } from "./RawPCMRecorder";

import { useState, useEffect, useCallback, useRef } from "react";
export type OnVolumeChange = (volume: number) => void;
export type OnDataAvailable = (data: Int16Array) => void;

export interface SilenceAwareRecorderOptions {
  deviceId?: string;
  minDecibels?: number;
  onDataAvailable?: OnDataAvailable;
  onVolumeChange?: OnVolumeChange;
  setDeviceId?: (deviceId: string) => void;
  silenceDuration?: number;
  silentThreshold?: number;
  maxInterval?: number;
}

export const usePCMRecorder = (options: SilenceAwareRecorderOptions) => {
  const {
    silenceDuration,
    silentThreshold,
    minDecibels,
    maxInterval = 7000,
    deviceId: initialDeviceId = "default",
  } = options;
  const recorderRef = useRef<RawPCMRecorder | null>(null);
  const [isRecording, setIsRecording] = useState<boolean>(false);
  const [cumulativeDataSent, setCumulativeDataSent] = useState<number>(0);

  // useEffect(() => {
  //   return () => {
  //     if (isRecording) {
  //       recorderRef.current?.stopRecording();
  //       recorderRef.current = null;
  //     }
  //   };
  // }, [options, isRecording]);

  const startRecording = useCallback(() => {
    const silenceAwareRecorder = new RawPCMRecorder(maxInterval);
    recorderRef.current = silenceAwareRecorder;
    if (options.onDataAvailable) {
      recorderRef.current.onData((message) => {
        if (message.type === "data") {
          options.onDataAvailable && options.onDataAvailable(message.data);
          setCumulativeDataSent((prev) => prev + message.data.length);
        } else if (message.type === "volume") {
          options.onVolumeChange && options.onVolumeChange(message.volume);
        }
      });
    }
    recorderRef.current.startRecording();
    setIsRecording(true);
  }, [recorderRef, options]);

  const stopRecording = useCallback(() => {
    recorderRef.current?.stopRecording();
    setIsRecording(false);
  }, [recorderRef]);

  return {
    startRecording,
    stopRecording,
    isRecording,
    cumulativeDataSent,
  };
};

export default usePCMRecorder;

/**
 * Gets the WAV file header information.
 */
function getWAVHeader({
  chunks,
  channel,
  sampleRate,
}: {
  chunks: ArrayBufferLike[];
  channel: number;
  sampleRate: number;
}): ArrayBuffer {
  const BYTES_PER_SAMPLE = Int16Array.BYTES_PER_ELEMENT;
  /**
   * Get stored encoding result with Wave file format header
   * Reference: http://www-mmsp.ece.mcgill.ca/Documents/AudioFormats/WAVE/WAVE.html
   */
  // Create header data
  const dataLength = chunks.reduce((acc, cur) => acc + cur.byteLength, 0);
  const header = new ArrayBuffer(44);
  const view = new DataView(header);
  // RIFF identifier 'RIFF'
  writeString(view, 0, "RIFF");
  // file length minus RIFF identifier length and file description length
  view.setUint32(4, 36 + dataLength, true);
  // RIFF type 'WAVE'
  writeString(view, 8, "WAVE");
  // format chunk identifier 'fmt '
  writeString(view, 12, "fmt ");
  // format chunk length
  view.setUint32(16, 16, true);
  // sample format (raw)
  view.setUint16(20, 1, true);
  // channel count
  view.setUint16(22, channel, true);
  // sample rate
  view.setUint32(24, sampleRate, true);
  // byte rate (sample rate * block align)
  view.setUint32(28, sampleRate * BYTES_PER_SAMPLE * channel, true);
  // block align (channel count * bytes per sample)
  view.setUint16(32, BYTES_PER_SAMPLE * channel, true);
  // bits per sample
  view.setUint16(34, 8 * BYTES_PER_SAMPLE, true);
  // data chunk identifier 'data'
  writeString(view, 36, "data");
  // data chunk length
  view.setUint32(40, dataLength, true);

  return header;
}

function exportFile({ chunks }: { chunks: ArrayBufferLike[] }) {
  if (chunks.length !== 0) {
    // 將 header 與 chunks 合併
    const wavRawData = [
      getWAVHeader({
        chunks,
        channel: 1,
        sampleRate: 16000,
      }),
      ...chunks,
    ];

    const link = document.createElement("a");
    const blob = new Blob(wavRawData, { type: "audio/wav" });
    const audioURL = URL.createObjectURL(blob);
    link.style.display = "none";
    link.href = audioURL;
    link.download = "sample.wav";
    document.body.appendChild(link);
    link.click();

    setTimeout(() => {
      document.body.removeChild(link);
      window.URL.revokeObjectURL(audioURL);
    }, 100);

    chunks = [];
  }
}

/**
 * Writes a string into a DataView object.
 *
 * @param {DataView} dataView - The DataView object to write the string into.
 * @param {number} offset - The offset in bytes.
 * @param {string} string - The string to write.
 */
function writeString(dataView: DataView, offset: number, string: string): void {
  for (let i = 0; i < string.length; i++) {
    dataView.setUint8(offset + i, string.charCodeAt(i));
  }
}
