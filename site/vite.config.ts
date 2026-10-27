// In dev, ./data/* is served from contract/examples and POST /api/trade is echoed back (nothing is saved).
import { readFile } from "node:fs/promises";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const examples = new URL("../contract/examples/", import.meta.url);

export default defineConfig({
  base: "./",
  plugins: [
    react(),
    {
      name: "dev-data",
      apply: "serve",
      configureServer(server) {
        server.middlewares.use("/data", async (req, res) => {
          const path = (req.url ?? "").split("?")[0].replace(/^\/+/, "");
          if (path.includes("..") || !path.endsWith(".json")) return void res.writeHead(404).end();
          try {
            const body = await readFile(new URL(path, examples));
            res.writeHead(200, { "Content-Type": "application/json" }).end(body);
          } catch {
            res.writeHead(404).end();
          }
        });
        server.middlewares.use("/api/trade", (req, res) => {
          let body = "";
          req.on("data", (c) => (body += c));
          req.on("end", () => {
            console.log("[dev] /api/trade", req.method, body);
            res.writeHead(req.method === "POST" ? 200 : 405, { "Content-Type": "application/json" });
            res.end(JSON.stringify({ dev: true }));
          });
        });
      },
    },
  ],
});
