"use client";
import React from "react";
import { FaStop, FaMicrophone } from "react-icons/fa";

export const RecordButton = ({
  handleRecording,
  isRecording,
  className = "",
}: {
  handleRecording: () => void;
  isRecording: boolean;
  className?: string;
}) => {
  return (
    <button className={`btn ${className}`} onClick={handleRecording}>
      <span style={{ display: "inline-block" }}>
        {isRecording ? "Stop Recording" : "Start Recording"}
      </span>
      &nbsp;
      <span style={{ display: "inline-block" }}>
        {isRecording ? <FaStop /> : <FaMicrophone />}
      </span>
    </button>
  );
};
