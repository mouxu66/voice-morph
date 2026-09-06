// 全局轻量通知（toast）系统：模块级单例，任何文件 import { notify } 即可用，
// 无需 Context 穿透。配合 App.tsx 挂载 <ToastViewport/> 与全局兜底监听，
// 让所有「未捕获异常 / 被 catch 但未提示」的失败都能立刻弹出来。
//
// 设计取舍：
// - 用户主动操作（按钮 → API 失败）的 catch 里调 notify.error，最显眼；
// - 轮询类 catch 保持静默（后端未就绪属正常，弹窗会刷屏）；
// - 全局 unhandledrejection / error 监听做兜底，覆盖所有漏网异常。
import { useEffect, useState } from "react";
import { AlertTriangle, CheckCircle2, Info, X, AlertCircle, Copy, Check } from "lucide-react";
import { friendlyError } from "./errors";

export type ToastType = "error" | "info" | "success" | "warn";
type ToastItem = { id: number; type: ToastType; title: string; detail?: string; created: number };

const listeners = new Set<(items: ToastItem[]) => void>();
let items: ToastItem[] = [];
let seq = 0;
// 同内容 2 秒内去重：防 dev StrictMode 双发、轮询抖动、同一错误反复触发
const recent = new Map<string, number>();

function emit() {
  for (const l of listeners) l(items);
}

function push(type: ToastType, rawTitle: string, detail?: string) {
  const title = type === "error" ? friendlyError(rawTitle) : rawTitle || "提示";
  const key = `${type}::${title}::${detail ?? ""}`;
  const now = Date.now();
  const last = recent.get(key);
  if (last && now - last < 2000) return;
  recent.set(key, now);
  // 顺手清过期条目，避免 Map 无限增长
  for (const [k, t] of recent) if (now - t > 2000) recent.delete(k);

  const item: ToastItem = { id: ++seq, type, title, detail, created: Date.now() };
  // 最多叠 6 条，避免刷屏；新的顶掉最旧的
  items = [...items, item].slice(-6);
  emit();
  const ttl = type === "error" ? 9000 : type === "warn" ? 6000 : 4000;
  window.setTimeout(() => dismiss(item.id), ttl);
}

export const notify = {
  error: (msg: string, detail?: string) => push("error", msg, detail),
  warn: (msg: string, detail?: string) => push("warn", msg, detail),
  info: (msg: string, detail?: string) => push("info", msg, detail),
  success: (msg: string, detail?: string) => push("success", msg, detail),
};

export function dismiss(id: number) {
  items = items.filter((i) => i.id !== id);
  emit();
}

// 全局兜底：任何未捕获异常 / Promise rejection，自动弹错误 toast。
// 覆盖「抛了但没人 catch」的绝大多数静默失败（用户点了按钮却没反应也没提示）。
// 注意：被 catch {} 吞掉的地方不会触发这里，那种需要显式调 notify.error。
if (typeof window !== "undefined") {
  const onReject = (e: PromiseRejectionEvent) => {
    const reason = e.reason instanceof Error ? e.reason.message : String(e.reason ?? "未知错误");
    notify.error(reason);
  };
  const onError = (e: ErrorEvent) => {
    if (e.message) notify.error(e.message);
  };
  window.addEventListener("unhandledrejection", onReject);
  window.addEventListener("error", onError);
}

export function ToastViewport() {
  const [list, setList] = useState<ToastItem[]>(items);
  useEffect(() => {
    const l = (it: ToastItem[]) => setList(it);
    listeners.add(l);
    setList(items);
    return () => {
      listeners.delete(l);
    };
  }, []);
  return (
    <div className="pointer-events-none fixed right-4 top-20 z-[100] flex w-[min(92vw,380px)] flex-col gap-2">
      {list.map((it) => (
        <ToastCard key={it.id} item={it} />
      ))}
    </div>
  );
}

const PALETTE = {
  error: { ring: "border-destructive/50 bg-destructive/10", bar: "bg-destructive", icon: <AlertTriangle className="h-4 w-4 text-destructive" /> },
  warn: { ring: "border-yellow-500/50 bg-yellow-500/10", bar: "bg-yellow-500", icon: <AlertCircle className="h-4 w-4 text-yellow-500" /> },
  info: { ring: "border-primary/40 bg-primary/10", bar: "bg-primary", icon: <Info className="h-4 w-4 text-primary" /> },
  success: { ring: "border-emerald-500/50 bg-emerald-500/10", bar: "bg-emerald-500", icon: <CheckCircle2 className="h-4 w-4 text-emerald-500" /> },
};

function ToastCard({ item }: { item: ToastItem }) {
  const [open, setOpen] = useState(false);
  const [copied, setCopied] = useState(false);
  const palette = PALETTE[item.type];
  const copy = async () => {
    const text = [item.title, item.detail].filter(Boolean).join("\n");
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      try {
        document.execCommand("copy");
      } catch {
        /* 放弃 */
      }
      ta.remove();
    }
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1500);
  };
  return (
    <div className={`pointer-events-auto relative overflow-hidden rounded-lg border ${palette.ring} px-3 py-2.5 shadow-lg backdrop-blur`}>
      <span className={`absolute inset-y-0 left-0 w-1 ${palette.bar}`} aria-hidden="true" />
      <div className="flex items-start gap-2 pl-1.5">
        <div className="mt-0.5 shrink-0">{palette.icon}</div>
        <div className="min-w-0 flex-1">
          <p className="text-sm font-semibold leading-5 text-foreground">{item.title}</p>
          {item.detail ? (
            <>
              <pre
                className={`mt-1 overflow-hidden whitespace-pre-wrap break-words font-mono text-xs leading-4 text-foreground/80 ${
                  open ? "max-h-60 overflow-auto" : "max-h-[3.5rem]"
                }`}
              >
                {item.detail}
              </pre>
              <button
                type="button"
                onClick={() => setOpen((o) => !o)}
                className="mt-1 text-xs text-muted-foreground underline-offset-2 hover:underline"
              >
                {open ? "收起" : "展开详情"}
              </button>
            </>
          ) : null}
        </div>
        <button
          type="button"
          onClick={copy}
          title="复制报错，可发给 AI 分析"
          className="shrink-0 rounded p-1 text-muted-foreground transition hover:bg-background/60 hover:text-foreground"
        >
          {copied ? <Check className="h-3.5 w-3.5" /> : <Copy className="h-3.5 w-3.5" />}
        </button>
        <button
          type="button"
          onClick={() => dismiss(item.id)}
          title="关闭"
          className="shrink-0 rounded p-1 text-muted-foreground transition hover:bg-background/60 hover:text-foreground"
        >
          <X className="h-3.5 w-3.5" />
        </button>
      </div>
    </div>
  );
}
