export interface ClientOptions { endpoint?: string; apiKey?: string; fetch?: typeof fetch }
export interface CreateOptions {
  image?: string; cpu?: number; memory?: string | number; disk?: string | number;
  timeout?: string | number; createTimeout?: number; env?: Record<string, string>;
  workdir?: string; network?: { internet?: boolean; privateNetwork?: false };
  metadata?: Record<string, string>; idempotencyKey?: string;
}
export interface SandboxInfo {
  id: string; state: "pending" | "running" | "destroyed" | "expired" | "failed" | "lost";
  image: string; cpu: number; memory: number; disk: number; node: string | null;
  created_at: string; expires_at: string; error: string | null; metadata: Record<string, string>;
}
export interface ExecOptions { timeout?: number; cwd?: string; env?: Record<string, string> }
export interface CommandResult {
  id: string; status: "running" | "exited" | "killed" | "timed_out" | "failed";
  exitCode: number | null; stdout: string; stderr: string; truncated: boolean;
  startedAt: string; finishedAt: string | null; error: string | null;
}
export interface CommandEvent {
  seq: number; type: "stdout" | "stderr" | "exit" | "truncated";
  text?: string; exitCode?: number; status?: CommandResult["status"];
}
export interface ImageInfo { reference: string; digest: string; architecture: string; size: number; build_seconds: number; created_at: string }
