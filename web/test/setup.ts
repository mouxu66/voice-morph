import '@testing-library/jest-dom';

/**
 * jsdom 没实现 `matchMedia`，而 `@/theme` **在模块顶层**就读它
 * （`const mql = window.matchMedia("(prefers-color-scheme: dark)")`）——
 * 于是任何 import 到 `theme` 的组件测试都会在收集阶段直接炸
 * （报 `window.matchMedia is not a function`，且一条用例都跑不起来）。
 *
 * 放全局而不是各测试文件里复制：这是 jsdom 缺的浏览器 API，不是某个被测组件的
 * 私有依赖。桩只实现 `theme.ts` 真正用到的三个口（`matches` / `addEventListener` /
 * `removeEventListener`），`matches` 恒为 false = 系统主题按亮色处理。
 */
if (!window.matchMedia) {
  window.matchMedia = ((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addEventListener: () => {},
    removeEventListener: () => {},
    addListener: () => {},
    removeListener: () => {},
    dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia;
}
