"use client";
import React from "react";
import {
  usePCMRecorder,
  encodeToB64,
  RecordButton,
  VolumeBar,
  LiveTitle,
  rawPCMInput,
  Transcripts,
} from "@livestack/transcribe-client";
import { useJobBinding, useOutput, useInput } from "@livestack/client";
import { SPEECH_LIVEFLOW_NAME } from "../common/defs";
import { translationOutputSchema } from "@livestack/lab-internal-common";

// SpeechComponents component
export const SpeechComponents: React.FC = () => {
  // Bind to a job using the specified live flow name
  const job = useJobBinding({
    specName: SPEECH_LIVEFLOW_NAME,
  });

  // Set up input feed for raw PCM data
  const { feed } = useInput({
    tag: "input-default",
    def: rawPCMInput,
    job,
  });

  // Set up output for translation data
  const translation = useOutput({
    tag: "translation",
    def: translationOutputSchema,
    job,
    query: { type: "lastN", n: 10 },
  });

  // State for volume level
  const [volume, setVolume] = React.useState<number>(0);

  // Set up PCM recorder with handlers for data and volume changes
  const { startRecording, stopRecording, isRecording, cumulativeDataSent } =
    usePCMRecorder({
      onDataAvailable: async (data) => {
        const encoded = encodeToB64(data);
        if (feed) {
          await feed({ rawPCM64Str: encoded });
          console.log(encoded.slice(0, 10), "length: ", encoded.length);
        }
      },
      onVolumeChange: (volume) => {
        setVolume(volume);
      },
    });

  // Toggle recording state
  const handleRecording = isRecording ? stopRecording : startRecording;

  return (
    <div className="m-4 grid grid-cols-5 gap-2 divide-x">
      <div>
        <h2 className="text-red-800">1. Click on "Start Recording" button</h2>
        <br />
        {job.jobId && (
          <RecordButton
            isRecording={isRecording}
            handleRecording={handleRecording}
            className="w-fit rounded border border-gray-800 bg-gray-200 p-2"
          />
        )}
        <VolumeBar volume={volume} cumulativeDataSent={cumulativeDataSent} />
      </div>
      <div className="col-span-2">
        <div className="ml-4">
          <h2 className="text-green-800">
            2. Speech transcripts will pop up here
          </h2>
          <br />
          <Transcripts
            tag="transcription"
            job={job}
            query={{ type: "lastN", n: 10 }}
          />
        </div>
      </div>
      <div className="col-span-2">
        <div className="ml-4">
          <h2 className="text-blue-800">
            3. Periodically, a one-liner short summary is generated
          </h2>
          <br />
          <LiveTitle tag="summarized-title" job={job} />
          <br />
          {translation && (
            <div>
              <h2 className="text-indigo-800">
                4. Your speech translated to French
              </h2>
              <br />
              {translation.map((t, idx) => (
                <div key={idx}>{t.data.translated}</div>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
};
export default SpeechComponents;
