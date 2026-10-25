import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  server: { host: "127.0.0.1", watch: { ignored: ["**/public/recording.json", "**/public/.recording.json.tmp"] } },
  preview: { host: "127.0.0.1" },
});
