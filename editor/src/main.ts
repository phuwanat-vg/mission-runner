import "./style.css";
import { App } from "./ui/App";

const root = document.getElementById("app");
if (!root) throw new Error("#app missing");

const app = new App(root);

// Vite HMR: tear down the old app (WebSocket, listeners) before the module is replaced.
if (import.meta.hot) {
  import.meta.hot.dispose(() => app.dispose());
}
