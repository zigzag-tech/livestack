"use client";
import React from "react";
import prettyBytes from "pretty-bytes";

export const VolumeBar = ({
  volume,
  cumulativeDataSent,
}: {
  volume: number;
  cumulativeDataSent?: number;
}) => {
  return (
    <div>
      Volume: <span>{volume.toFixed(1)}</span>
      <br />
      <progress value={volume} max={100} style={{ width: "100px" }}></progress>
      <br />
      {typeof cumulativeDataSent !== "undefined" && (
        <>Total data sent: {prettyBytes(cumulativeDataSent)}</>
      )}
    </div>
  );
};
