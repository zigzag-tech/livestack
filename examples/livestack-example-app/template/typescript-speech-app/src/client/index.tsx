import React, { Suspense } from "react";
import ReactDOM from "react-dom/client";
import SpeechComponents from "./SpeechComponents";
import "./globals.css";

const root = ReactDOM.createRoot(
  document.getElementById("root") as HTMLElement
);

root.render(
  <Suspense fallback={<div>Loading...</div>}>
    <SpeechComponents />
  </Suspense>
);
