"use client";

import React, { useRef, useEffect, useState } from "react";

export default function SpeechTest() {
  const [textValue, setTextValue] = useState("Hello, this is a test.");
  return (
    <div>
      <h1>Speech Test: No debounce implemented, so copy and paste your entire content at once</h1>
      <textarea
        className="mb-2 p-2 border rounded-md shadow-sm"
        value={textValue}
        onChange={(e) => setTextValue(e.target.value)}
        rows={4}
      />
      <audio
        src={`http://localhost:3000/api/stream-audio?text=${encodeURIComponent(
          textValue
        )}`}
        controls
      />
    </div>
  );
}
