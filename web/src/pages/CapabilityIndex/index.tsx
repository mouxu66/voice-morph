import { CapabilityIndexRoute as Page } from "./CapabilityIndexPage"

/**
 * 页面具名导出（本仓约定：`pages/<X>/index.tsx` 导出 `<X>Route`，没有 default）。
 *
 * 写成 `export const X = Page` 而不是 `export { Page as X }`：
 * 后端门禁 `test_declared_page_module_and_export_exist` 用正则认
 * `export (function|const) <名>` 的具名导出，export 子句会被判「没导出」而假红。
 */
export const CapabilityIndexRoute = Page
