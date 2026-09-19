import { afterEach, describe, expect, it, vi } from "vitest";
import { mkdtemp, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { list } from "tar";
import { APIError, OpenSandbox } from "../src/index.js";
import { pack, unpack, readBounded } from "../src/files.js";

const directories: string[] = [];
afterEach(async () => { for (const directory of directories.splice(0)) await rm(directory, { recursive: true, force: true }); });
async function temporary() { const directory = await mkdtemp(path.join(tmpdir(), "opensandbox-test-")); directories.push(directory); return directory; }
const info = { id: "sb-123", state: "running", image: "python:3.13", cpu: 1, memory: 1e9, disk: 1e10, node: "worker", created_at: "2026-01-01T00:00:00Z", expires_at: "2026-01-01T01:00:00Z", error: null, metadata: {} };
const command = { id: "cmd-1", status: "exited", exit_code: 7, stdout: "hello", stderr: "problem", truncated: false, started_at: info.created_at, finished_at: info.created_at, error: null };
function fixture(handler: (url: string, options: RequestInit) => Response | Promise<Response>) {
  const calls: Array<{ url: string; options: RequestInit }> = [];
  const fetcher = vi.fn(async (input: string | URL | Request, options?: RequestInit) => {
    const url = String(input); calls.push({ url, options: options ?? {} });
    return handler(url, options ?? {});
  }) as unknown as typeof fetch;
  return { client: new OpenSandbox({ endpoint: "http://head:7070", apiKey: "secret", fetch: fetcher }), calls };
}

describe("OpenSandbox SDK", () => {
  it("authenticates, sends resource options, and maps command results", async () => {
    const { client, calls } = fixture((url) => Response.json(url.endsWith("/commands") ? command : info));
    const sandbox = await client.create({ cpu: 2, memory: "4gb", disk: "20gb", createTimeout: 150, idempotencyKey: "retry-1" });
    const result = await sandbox.exec("exit 7");
    expect(result.exitCode).toBe(7); expect(result.stdout).toBe("hello");
    const headers = new Headers(calls[0].options.headers);
    expect(headers.get("Authorization")).toBe("Bearer secret");
    expect(headers.get("Idempotency-Key")).toBe("retry-1");
    expect(JSON.parse(String(calls[0].options.body))).toMatchObject({ cpu: 2, memory: "4gb", disk: "20gb", create_timeout: 150 });
    expect(calls[0].options.redirect).toBe("error");
  });
  it("exposes structured API errors", async () => {
    const { client } = fixture(() => Response.json({ error: { code: "capacity_timeout", message: "No capacity" } }, { status: 503 }));
    await expect(client.create()).rejects.toMatchObject({ name: "APIError", code: "capacity_timeout", status: 503 });
  });
  it("preserves split UTF-8 characters and final status in SSE logs", async () => {
    const encoder = new TextEncoder();
    const bytes = encoder.encode('data: {"seq":0,"type":"stdout","text":"🙂"}\n\ndata: {"seq":1,"type":"exit","exit_code":0}\n\n');
    const stream = new ReadableStream<Uint8Array>({ start(controller) { for (const byte of bytes) controller.enqueue(Uint8Array.of(byte)); controller.close(); } });
    const { client } = fixture(url => url.includes("/events") ? new Response(stream) : Response.json(url.endsWith("/commands") ? command : info));
    const sandbox = await client.get("sb-123"); const background = await sandbox.execBackground("printf hello");
    const events = []; for await (const event of background.streamLogs()) events.push(event);
    expect(events[0].text).toBe("🙂"); expect(events[1].exitCode).toBe(0);
  });
  it("uploads a local image context before creating the sandbox", async () => {
    const directory = await temporary(); await writeFile(path.join(directory, "Dockerfile"), "FROM python:3.13\n");
    const { client, calls } = fixture(url => Response.json(url.includes("/images/build") ? { reference: "head/image@sha256:123" } : info));
    await client.create({ image: directory });
    expect(calls[0].url).toContain("/images/build?dockerfile=Dockerfile");
    expect(JSON.parse(String(calls[1].options.body)).image).toBe("head/image@sha256:123");
  });
});

describe("transfer archives", () => {
  it("cancels oversized streaming downloads before reading the whole response", async () => {
    let pulls = 0; let cancelled = false;
    const stream = new ReadableStream<Uint8Array>({
      pull(controller) { pulls++; controller.enqueue(Uint8Array.of(1, 2, 3, 4)); },
      cancel() { cancelled = true; },
    });
    await expect(readBounded(new Response(stream), 8)).rejects.toThrow("Download exceeds");
    expect(pulls).toBeLessThanOrEqual(4); expect(cancelled).toBe(true);
  });
  it("respects Docker ignore anchoring and negated descendants", async () => {
    const directory = await temporary();
    await mkdir(path.join(directory, "nested")); await mkdir(path.join(directory, "ignored"));
    await writeFile(path.join(directory, "Dockerfile"), "FROM scratch");
    await writeFile(path.join(directory, ".dockerignore"), "secret\nignored\n!ignored/include\n");
    await writeFile(path.join(directory, "secret"), "private");
    await writeFile(path.join(directory, "nested/secret"), "nested allowed");
    await writeFile(path.join(directory, "ignored/include"), "include");
    await writeFile(path.join(directory, "ignored/exclude"), "exclude");
    const { data } = await pack(directory, true); const names: string[] = [];
    await new Promise<void>((resolve, reject) => {
      const parser = list({ onReadEntry: entry => names.push(entry.path) }); parser.on("error", reject); parser.on("end", resolve); parser.end(Buffer.from(data));
    });
    expect(names).not.toContain("secret"); expect(names).toContain("nested/secret");
    expect(names).toContain("ignored/include"); expect(names).not.toContain("ignored/exclude");
  });
  it("roundtrips binary files and empty directories", async () => {
    const source = await temporary(); const destination = await temporary();
    await mkdir(path.join(source, "empty")); await writeFile(path.join(source, "binary"), Buffer.from([0, 255, 10]));
    const { data } = await pack(source); await unpack(data, destination);
    expect(await readFile(path.join(destination, "binary"))).toEqual(Buffer.from([0, 255, 10]));
  });
});
