import React from "react";
import ReactDOM from "react-dom/client";
import { QueryClientProvider } from "@tanstack/react-query";
import "./index.css";
import App from "./App";
import { queryClient } from "./api/queryClient";
import { VocabGate } from "./components/VocabGate";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <VocabGate>
        <App />
      </VocabGate>
    </QueryClientProvider>
  </React.StrictMode>,
);
