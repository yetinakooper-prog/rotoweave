import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import "./styles.css";
import "./responsive.css";
import "./accessibility.css";
import "./model-compatibility.css";

const client = new QueryClient({
  defaultOptions: { queries: { refetchInterval: 2000, retry: 2 } },
});

createRoot(document.getElementById("root")!).render(
  <StrictMode><QueryClientProvider client={client}><App /></QueryClientProvider></StrictMode>,
);
