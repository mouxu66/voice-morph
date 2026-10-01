/**
 * 手机遥控二维码卡片 —— issue #1 的对症方案。
 *
 * 背景（盘点 2026-10-01 改判）：手机浏览器做不了 mDNS 服务发现（网页没有
 * Bonjour 能力，`.local` 在安卓浏览器上也不可靠），所以「自动发现」在浏览器端
 * 走不通；对症的是桌面端把遥控页完整地址（含 `?api_key=`）生成二维码，
 * 手机扫码即开 —— 不用手填 IP，也就不需要 issue #1 里 Expo App 的 zeroconf。
 *
 * 数据来自 `GET /api/system/lan`（IP 判据 = RFC1918，见 system_api.py：
 * 本机跑 Mihomo TUN + Radmin VPN，"第一个非回环地址"多半是虚拟网卡的假地址）。
 */
import { Smartphone } from "lucide-react";
import { renderSVG } from "uqr";
import { useEffect, useMemo, useState } from "react";
import { getLan } from "@/api/client";
import { Card } from "@/components/layout/PageShell";

/** 四态：拉取中 / 有地址 / 明确没地址 / 请求本身失败。 */
type State =
  | { kind: "loading" }
  | { kind: "ready"; url: string; ip: string | null; tokenSet: boolean; note: string }
  | { kind: "no-lan" }
  | { kind: "error"; message: string };

/**
 * uqr 的 renderSVG 出**整段** `<svg>`（白底黑块，含 quiet zone）。
 * 内容由本库从同一个 state.url 字符串生成 —— 展示文本与二维码永远同源，
 * 不会出现「扫出来和看到的不一样」。
 * dangerouslySetInnerHTML 的输入是 uqr 纯几何生成、无任何用户可控片段，安全。
 */
function QrSvg({ text, size = 148 }: { text: string; size?: number }) {
  const svg = useMemo(() => renderSVG(text, { ecc: "M", border: 4 }), [text]);
  return (
    <span
      role="img"
      aria-label="遥控页地址二维码"
      className="block shrink-0 overflow-hidden rounded-lg bg-white p-1 shadow-sm [&>svg]:h-full [&>svg]:w-full"
      style={{ width: size, height: size }}
      dangerouslySetInnerHTML={{ __html: svg }}
    />
  );
}

export function RemoteQrCard() {
  const [state, setState] = useState<State>({ kind: "loading" });
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    let alive = true;
    getLan()
      .then((lan) => {
        if (!alive) return;
        if (lan.url) {
          setState({ kind: "ready", url: lan.url, ip: lan.ip, tokenSet: lan.token_set, note: lan.note });
        } else {
          setState({ kind: "no-lan" });
        }
      })
      .catch((e: unknown) => {
        if (alive) setState({ kind: "error", message: e instanceof Error ? e.message : String(e) });
      });
    return () => {
      alive = false;
    };
  }, []);

  async function copy() {
    if (state.kind !== "ready") return;
    try {
      await navigator.clipboard.writeText(state.url);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      /* 剪贴板不可用就算了 —— URL 本来就完整显示着 */
    }
  }

  return (
    <Card tone="raised" className="mt-6 p-5" data-testid="remote-qr-card">
      <div className="flex items-start gap-4">
        <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl border border-primary/30 bg-primary/10 text-primary">
          <Smartphone className="h-5 w-5" />
        </span>
        <div className="min-w-0 flex-1">
          <h2 className="text-sm font-semibold text-card-foreground">手机遥控</h2>
          {state.kind === "loading" && (
            <p className="mt-1 text-xs leading-5 text-muted-foreground">正在获取局域网地址…</p>
          )}

          {state.kind === "ready" && (
            <div className="mt-2 flex items-start gap-4">
              <QrSvg text={state.url} />
              <div className="min-w-0 flex-1 text-xs leading-5 text-muted-foreground">
                <p>
                  手机与电脑连{" "}
                  <strong className="font-medium text-foreground">同一个 Wi-Fi</strong>
                  ，用相机扫码直接打开遥控页 —— 不用手填 IP。没有相机就手动输入下面的地址。
                </p>
                <p className="mt-2 break-all rounded-md border border-border bg-background/60 px-2 py-1.5 font-mono text-[11px] text-foreground">
                  {state.url}
                </p>
                <div className="mt-2 flex items-center gap-2">
                  <button
                    type="button"
                    onClick={copy}
                    className="rounded-md border border-border px-2 py-1 text-[11px] font-medium text-foreground transition hover:border-primary hover:text-primary"
                  >
                    {copied ? "已复制" : "复制地址"}
                  </button>
                  {state.tokenSet ? (
                    <span className="text-[11px]">地址里已带上访问密钥，别发到群里。</span>
                  ) : (
                    <span className="text-[11px] text-amber-600 dark:text-amber-400">{state.note}</span>
                  )}
                </div>
              </div>
            </div>
          )}

          {state.kind === "no-lan" && (
            <p className="mt-1 text-xs leading-5 text-muted-foreground">
              没找到局域网地址 —— 电脑没插网线 / 没连 Wi-Fi，或只有虚拟网卡在跑。
              连上网络后刷新本页再试。
            </p>
          )}

          {state.kind === "error" && (
            <p className="mt-1 text-xs leading-5 text-muted-foreground">
              拿不到局域网信息（{state.message}）—— 后端没在跑？启动变声工坊后再试。
            </p>
          )}
        </div>
      </div>
    </Card>
  );
}
