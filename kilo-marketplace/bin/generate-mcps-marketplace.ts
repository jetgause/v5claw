#!/usr/bin/env npx tsx
/**
 * Generate marketplace.yaml from individual MCP directories.
 *
 * Usage: npx tsx bin/generate-mcps-marketplace.ts
 */

import * as fs from "fs";
import * as path from "path";
import { fileURLToPath } from "url";
import * as yaml from "yaml";
import { Document } from "yaml";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const mcpsDir = path.join(__dirname, "..", "mcps");

const items = fs
  .readdirSync(mcpsDir, { withFileTypes: true })
  .filter((d) => d.isDirectory() && !d.name.startsWith("."))
  .map((dir) => {
    const content = fs.readFileSync(path.join(mcpsDir, dir.name, "MCP.yaml"), "utf-8");
    const mcp = yaml.parse(content);
    console.log(`Added: ${mcp.name}`);
    return mcp;
  })
  .sort((a, b) => a.id.localeCompare(b.id));

const doc = new Document({ items });
const output = doc.toString({ lineWidth: 120 });

fs.writeFileSync(path.join(mcpsDir, "marketplace.yaml"), output);

console.log(`\nGenerated marketplace.yaml with ${items.length} MCPs`);
