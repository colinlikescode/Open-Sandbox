import { access, lstat, mkdir, readdir, readFile } from "node:fs/promises";
import path from "node:path";
import { create, extract } from "tar";
import { createRequire } from "node:module";
interface DockerIgnore { add(patterns: string): DockerIgnore; ignores(path: string): boolean }
const dockerignore = createRequire(import.meta.url)("@balena/dockerignore") as (options: { ignorecase: boolean }) => DockerIgnore;

export const MAX_TRANSFER = 512 * 1024 * 1024;
export async function readBounded(response: Response, limit = MAX_TRANSFER): Promise<Uint8Array> {
  if (!response.body) return new Uint8Array();
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = []; let size = 0;
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > limit) throw new Error("Download exceeds transfer limit");
      chunks.push(value);
    }
    return Buffer.concat(chunks);
  } finally {
    await reader.cancel();
    reader.releaseLock();
  }
}

export async function exists(name: string): Promise<boolean> {
  try { await access(name); return true; } catch { return false; }
}
export async function pack(local: string, context = false): Promise<{ data: Uint8Array; dockerfile?: string }> {
  const source = path.resolve(local);
  const stat = await lstat(source);
  if (stat.isSymbolicLink()) throw new Error("Transfers do not follow symlinks");
  const root = stat.isDirectory() ? source : path.dirname(source);
  const dockerfile = context ? stat.isDirectory() ? "Dockerfile" : path.basename(source) : undefined;
  if (dockerfile && !await exists(path.join(root, dockerfile))) throw new Error("Project context must contain a Dockerfile");
  const ignore = dockerignore({ ignorecase: false });
  if (context) {
    let ignorePath = path.join(root, `${dockerfile}.dockerignore`);
    if (!await exists(ignorePath)) ignorePath = path.join(root, ".dockerignore");
    if (await exists(ignorePath)) ignore.add(await readFile(ignorePath, "utf8"));
  }
  const entries: string[] = [];
  let total = 0;
  async function walk(directory: string): Promise<void> {
    for (const name of (await readdir(directory)).sort()) {
      const absolute = path.join(directory, name);
      const relative = path.relative(root, absolute).split(path.sep).join("/");
      if (context && relative.split("/").some(p => [".git", ".venv", ".cache", "node_modules"].includes(p))) continue;
      const info = await lstat(absolute);
      const excluded = context && relative !== dockerfile && ignore.ignores(relative);
      if (info.isDirectory()) {
        // Walk excluded directories too: Docker negations can re-include descendants.
        await walk(absolute);
        if (!excluded && (await readdir(absolute)).length === 0) entries.push(relative);
      } else if (!excluded) {
        if (!info.isFile()) throw new Error(`Cannot transfer symlink or special file: ${relative}`);
        total += info.size;
        if (total > MAX_TRANSFER) throw new Error("Transfer exceeds 512 MiB");
        entries.push(relative);
      }
    }
  }
  if (stat.isDirectory() || context) await walk(root);
  else { if (stat.size > MAX_TRANSFER) throw new Error("Transfer exceeds 512 MiB"); entries.push(path.basename(source)); }
  const archive = create({ cwd: root, portable: true, noMtime: true, follow: false, noDirRecurse: true }, entries);
  const chunks: Uint8Array[] = []; let size = 0;
  for await (const chunk of archive) {
    const data = Buffer.from(chunk); size += data.length;
    if (size > MAX_TRANSFER) throw new Error("Archive exceeds 512 MiB");
    chunks.push(data);
  }
  return { data: Buffer.concat(chunks), dockerfile };
}
export async function unpack(data: Uint8Array, destination: string): Promise<void> {
  if (data.byteLength > MAX_TRANSFER) throw new Error("Download exceeds 512 MiB");
  const root = path.resolve(destination);
  await mkdir(root, { recursive: true });
  let expanded = 0; let count = 0;
  const writer = extract({ cwd: root, strict: true, preservePaths: false,
    filter: (name, entry) => {
      if (++count > 100000) throw new Error("Too many archive entries");
      const target = path.resolve(root, name);
      if (path.isAbsolute(name) || (target !== root && !target.startsWith(root + path.sep))) throw new Error("Archive escapes destination");
      if (!("type" in entry) || (entry.type !== "File" && entry.type !== "Directory")) throw new Error("Archive contains a link or special file");
      expanded += entry.size ?? 0;
      if (expanded > MAX_TRANSFER) throw new Error("Expanded archive exceeds 512 MiB");
      return true;
    },
  });
  await new Promise<void>((resolve, reject) => {
    writer.on("error", reject); writer.on("close", resolve); writer.end(Buffer.from(data));
  });
}
