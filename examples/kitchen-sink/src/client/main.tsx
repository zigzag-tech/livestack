import "./index.scss";

import React, { Suspense } from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter as Router, Routes, Route } from 'react-router-dom';

import App from "./App";
import SpeechTest from "./SpeechTest";

ReactDOM.createRoot(document.getElementById("root") as HTMLElement).render(
  <React.StrictMode>
    <Router>
      <Suspense fallback={<>...</>}>
        <Routes>
          <Route path="/" element={<App />} />
          <Route path="/speech-test" element={<SpeechTest />} />
        </Routes>
      </Suspense>
    </Router>
  </React.StrictMode>
);
