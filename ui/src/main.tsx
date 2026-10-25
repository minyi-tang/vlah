import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import App from "./App.tsx";
import "./styles.css";
import "./night.css";
import "./replay.css";

const root_element = document.getElementById("root");
if (!root_element) {
  throw new Error("The observatory root element is missing.");
} else {
  createRoot(root_element).render(<StrictMode><App /></StrictMode>);
}
