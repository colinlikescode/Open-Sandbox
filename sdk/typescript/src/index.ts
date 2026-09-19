import { lstat, readFile } from "node:fs/promises";
import { homedir } from "node:os";
import { exists, pack, unpack, readBounded, MAX_TRANSFER } from "./files.js";
import type { ClientOptions, CommandEvent, CommandResult, CreateOptions, ExecOptions, ImageInfo, SandboxInfo } from "./types.js";
export type * from "./types.js";

export class APIError extends Error {
  constructor(public readonly status: number, public readonly code: string, message: string) { super(message); this.name = "APIError"; }
}
function result(raw: Record<string, unknown>): CommandResult {
  return { id: String(raw.id), status: raw.status as CommandResult["status"], exitCode: raw.exit_code as number | null,
    stdout: String(raw.stdout ?? ""), stderr: String(raw.stderr ?? ""), truncated: Boolean(raw.truncated),
    startedAt: String(raw.started_at), finishedAt: raw.finished_at as string | null, error: raw.error as string | null };
}
function expand(local: string): string { return local.startsWith("~/") ? homedir() + local.slice(1) : local; }

export class OpenSandbox {
  readonly endpoint: string;
  private readonly apiKey: string;
  private readonly fetcher: typeof fetch;
  constructor(options: ClientOptions = {}) {
    this.endpoint = (options.endpoint ?? process.env.OPENSANDBOX_ENDPOINT ?? "").replace(/\/$/, "");
    this.apiKey = options.apiKey ?? process.env.OPENSANDBOX_API_KEY ?? "";
    this.fetcher = options.fetch ?? globalThis.fetch;
    if (!this.endpoint || !this.apiKey) throw new Error("Set OPENSANDBOX_ENDPOINT and OPENSANDBOX_API_KEY, or pass endpoint and apiKey");
    const url = new URL(this.endpoint);
    if (!["http:", "https:"].includes(url.protocol) || url.username || url.password) throw new Error("endpoint must be an HTTP(S) URL without embedded credentials");
  }
  async request(path: string, options: RequestInit = {}): Promise<Response> {
    const headers = new Headers(options.headers);
    headers.set("Authorization", `Bearer ${this.apiKey}`);
    const response = await this.fetcher(this.endpoint + path, { ...options, headers, redirect: "error" });
    if (!response.ok) {
      const payload = await response.json().catch(() => ({})) as { error?: { code?: string; message?: string } };
      throw new APIError(response.status, payload.error?.code ?? "api_error", payload.error?.message ?? `API request failed (${response.status})`);
    }
    return response;
  }
  async json<T>(path: string, method = "GET", body?: unknown, headers?: Record<string, string>): Promise<T> {
    return (await this.request(path, { method, headers: { "Content-Type": "application/json", ...headers },
      body: body === undefined ? undefined : JSON.stringify(body) })).json() as Promise<T>;
  }
  async create(options: CreateOptions = {}): Promise<Sandbox> {
    const { idempotencyKey, createTimeout, ...body } = options;
    let image = options.image ?? "python:3.13-slim";
    if (/^(\.\.?\/|\/|~\/)/.test(image) || await exists(image)) image = (await this.build(expand(image))).reference;
    const info = await this.json<SandboxInfo>("/v1/sandboxes", "POST", { ...body, image, create_timeout: createTimeout },
      idempotencyKey ? { "Idempotency-Key": idempotencyKey } : undefined);
    return new Sandbox(this, info);
  }
  async get(id: string): Promise<Sandbox> { return new Sandbox(this, await this.json<SandboxInfo>(`/v1/sandboxes/${encodeURIComponent(id)}`)); }
  async list(): Promise<SandboxInfo[]> { return this.json("/v1/sandboxes"); }
  async build(local: string): Promise<ImageInfo> {
    const { data, dockerfile } = await pack(expand(local), true);
    return (await this.request(`/v1/images/build?dockerfile=${encodeURIComponent(dockerfile!)}`, {
      method: "POST", headers: { "Content-Type": "application/x-tar" }, body: data as BodyInit,
    })).json() as Promise<ImageInfo>;
  }
  async images(): Promise<unknown> { return this.json("/v1/images"); }
  async status(): Promise<unknown> { return this.json("/v1/status"); }
  async nodes(): Promise<unknown> { return this.json("/v1/nodes"); }
  async doctor(): Promise<{ ok: boolean; checks: Record<string, boolean> }> { return this.json("/v1/doctor", "POST"); }
  async metrics(): Promise<unknown> { return this.json("/v1/metrics"); }
}

export class Sandbox {
  readonly id: string;
  readonly path: string;
  constructor(readonly client: OpenSandbox, public info: SandboxInfo) {
    this.id = info.id; this.path = `/v1/sandboxes/${encodeURIComponent(this.id)}`;
  }
  async refresh(): Promise<SandboxInfo> { this.info = await this.client.json(this.path); return this.info; }
  async exec(command: string | string[], options: ExecOptions = {}): Promise<CommandResult> {
    return result(await this.client.json(this.path + "/commands", "POST", { ...options, command, background: false }));
  }
  async execBackground(command: string | string[], options: ExecOptions = {}): Promise<Command> {
    return new Command(this, result(await this.client.json(this.path + "/commands", "POST", { ...options, command, background: true })));
  }
  async listProcesses(): Promise<CommandResult[]> {
    return (await this.client.json<Record<string, unknown>[]>(this.path + "/commands")).map(result);
  }
  async killProcess(id: string): Promise<CommandResult> { return result(await this.client.json(`${this.path}/commands/${encodeURIComponent(id)}`, "DELETE")); }
  async write(path: string, content: string | Uint8Array): Promise<void> {
    const data = typeof content === "string" ? new TextEncoder().encode(content) : content;
    if (data.byteLength > MAX_TRANSFER) throw new Error("File exceeds 512 MiB");
    await this.client.request(`${this.path}/files?path=${encodeURIComponent(path)}`, { method: "PUT", body: data as BodyInit });
  }
  async read(path: string): Promise<string> { return new TextDecoder().decode(await this.readBytes(path)); }
  async readBytes(path: string): Promise<Uint8Array> { return readBounded(await this.client.request(`${this.path}/files?path=${encodeURIComponent(path)}`)); }
  async upload(local: string, remote: string): Promise<void> {
    local = expand(local);
    const stat = await lstat(local);
    if (stat.isFile()) {
      if (stat.size > MAX_TRANSFER) throw new Error("File exceeds 512 MiB");
      await this.write(remote, await readFile(local));
    } else {
      const { data } = await pack(local);
      await this.client.request(`${this.path}/archive?path=${encodeURIComponent(remote)}`, { method: "PUT", body: data as BodyInit });
    }
  }
  async download(remote: string, local: string): Promise<void> {
    const response = await this.client.request(`${this.path}/archive?path=${encodeURIComponent(remote)}`);
    await unpack(await readBounded(response), expand(local));
  }
  async getUrl(port: number, ttl = 3600): Promise<string> {
    return (await this.client.json<{ url: string }>(`${this.path}/ports/${port}?ttl=${ttl}`, "POST")).url;
  }
  async setTimeout(timeout: string | number): Promise<SandboxInfo> {
    this.info = await this.client.json(this.path + "/timeout", "PUT", { timeout }); return this.info;
  }
  async destroy(): Promise<SandboxInfo> { this.info = await this.client.json(this.path, "DELETE"); return this.info; }
}

export class Command {
  readonly id: string;
  readonly path: string;
  constructor(readonly sandbox: Sandbox, public info: CommandResult) {
    this.id = info.id; this.path = `${sandbox.path}/commands/${encodeURIComponent(this.id)}`;
  }
  async refresh(): Promise<CommandResult> { this.info = result(await this.sandbox.client.json(this.path)); return this.info; }
  async wait(): Promise<CommandResult> {
    while ((await this.refresh()).finishedAt === null) await new Promise(resolve => setTimeout(resolve, 100));
    return this.info;
  }
  async kill(): Promise<CommandResult> { return this.sandbox.killProcess(this.id); }
  async *streamLogs(fromSeq = 0): AsyncGenerator<CommandEvent> {
    const response = await this.sandbox.client.request(`${this.path}/events?from_seq=${fromSeq}`);
    if (!response.body) return;
    const reader = response.body.getReader(); const decoder = new TextDecoder(); let buffer = "";
    try {
      while (true) {
        const { done, value } = await reader.read();
        buffer += done ? decoder.decode() : decoder.decode(value, { stream: true });
        let end: number;
        while ((end = buffer.indexOf("\n")) >= 0) {
          const line = buffer.slice(0, end).replace(/\r$/, ""); buffer = buffer.slice(end + 1);
          if (line.startsWith("data: ")) {
            const raw = JSON.parse(line.slice(6));
            yield { seq: raw.seq, type: raw.type, text: raw.text, exitCode: raw.exit_code, status: raw.status };
          }
        }
        if (done) break;
      }
    } finally { await reader.cancel(); reader.releaseLock(); }
  }
}
