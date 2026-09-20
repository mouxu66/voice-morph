// 能力清单门控：拉一次 GET /api/plugins，对可关插件支撑的 tab/入口做显隐。
//
// 语义与 web/src/lib/pluginRoutes.tsx 的 pluginVisible 保持一致：
//   - 拿不到目录（后端没起来 / 接口出错）→ fail-open 全部显示
//   - 目录里找不到该 id → 显示
//   - core.* 恒显示
//   - 只看 enabled、不看 state：被别的启用中能力依赖而保留的能力是
//     state=disabled / enabled=true（路由仍挂着），拿 state 判会把还在的能力藏掉
import { useEffect, useState } from "react";

import { getPlugins, type PluginCatalog, type PluginEntry } from "@/src/api";
import { useAppStore } from "@/src/store";

function isVisible(p: PluginEntry): boolean {
  return Boolean(p.core) || p.enabled !== false;
}

/** 插件是否在界面上出现；目录为 null/缺 id 时 fail-open 返回 true。 */
export function pluginVisible(catalog: PluginCatalog | null | undefined, id: string): boolean {
  if (!catalog || !Array.isArray(catalog.plugins)) return true;
  const p = catalog.plugins.find((x) => x.id === id);
  return p ? isVisible(p) : true;
}

// 会话内缓存，按 host 分键 —— 设置页可改后端地址，改完要针对新 host 重拉。
let cached: { host: string; catalog: PluginCatalog } | null = null;
let inflight: { host: string; promise: Promise<PluginCatalog> } | null = null;

function loadCatalog(host: string): Promise<PluginCatalog> {
  if (cached && cached.host === host) return Promise.resolve(cached.catalog);
  if (inflight && inflight.host === host) return inflight.promise;
  const promise = getPlugins()
    .then((c) => {
      cached = { host, catalog: c };
      return c;
    })
    .finally(() => {
      if (inflight && inflight.host === host) inflight = null;
    });
  inflight = { host, promise };
  return promise;
}

/** 读一次能力目录。返回 null 表示「还没拿到 / 拿不到」，调用方按全部显示处理。 */
export function useCapabilities(): PluginCatalog | null {
  const host = useAppStore((s) => s.host);
  const [catalog, setCatalog] = useState<PluginCatalog | null>(() =>
    cached && cached.host === host ? cached.catalog : null,
  );

  useEffect(() => {
    if (cached && cached.host === host) {
      setCatalog(cached.catalog);
      return;
    }
    let alive = true;
    setCatalog(null);
    loadCatalog(host)
      .then((c) => {
        if (alive) setCatalog(c);
      })
      .catch(() => {
        /* 失败 = 拿不到目录 → 保持 null（全部显示） */
      });
    return () => {
      alive = false;
    };
  }, [host]);

  return catalog;
}