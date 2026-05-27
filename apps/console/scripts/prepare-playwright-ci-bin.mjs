import { chmod, copyFile, mkdir, rm } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const consoleRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const binDir = resolve(consoleRoot, "node_modules", ".bin");
const wrapperPath = resolve(consoleRoot, "scripts", "playwright-ci-wrapper.cjs");
const binPath = resolve(binDir, "playwright");

await mkdir(binDir, { recursive: true });
await rm(binPath, { force: true });
await copyFile(wrapperPath, binPath);
await chmod(binPath, 0o755);
