import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import CortexDashboard from "./CortexDashboard.jsx";
import "./cortex-dashboard.css";
import "./functional-controls.css";

createRoot(document.getElementById("root")).render(
  <StrictMode>
    <CortexDashboard />
  </StrictMode>,
);
