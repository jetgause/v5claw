#!/usr/bin/env node

/**
 * Main entry point for MCP stdio script
 * For backward compatibility, runs all tools by default
 */

import { spawn } from "child_process";
import path from "path";
import { fileURLToPath } from "url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const serverPath = path.join(__dirname, "server.js");

// Start the server with all tools (for backward compatibility)
const server = spawn("node", [serverPath, "all"], {
  stdio: "inherit",
});

// Handle server process events
server.on("error", (err) => {
  console.error(`Failed to start MCP server: ${err.message}`);
  process.exit(1);
});

server.on("exit", (code) => {
  if (code !== 0) {
    console.error(`MCP server exited with code ${code}`);
    process.exit(code || 1);
  }
  process.exit(0);
});

// Forward signals to the child process
["SIGINT", "SIGTERM"].forEach((signal) => {
  process.on(signal, () => {
    server.kill();
  });
});
