import js from '@eslint/js';
import tseslint from 'typescript-eslint';
import prettierConfig from 'eslint-config-prettier';
import reactHooks from 'eslint-plugin-react-hooks';

/**
 * ESLint 只管**代码质量**，不管排版 —— 排版交给 `npm run format`（prettier 单独跑）。
 *
 * 为什么不再用 `eslint-plugin-prettier` 把排版塞进 lint（2026-09-20 实测后改掉）：
 * prettier 官方就不推荐这个插件（每个文件多跑一遍解析，还把排版问题伪装成 lint 错误）。
 * 更实际的原因是**本仓库从来没被 prettier 格式化过**：开着 `prettier/prettier: error` 时
 * lint 报 3373 个错误，几乎全是 `Delete ';'` / 拆长 import，**没有一个是真缺陷**；
 * 关掉后只剩 15 个真问题。让 88 个文件（约 -3444 / +7950 行）为排版器让路不值得。
 *
 * 这里用 `eslint-config-prettier` 而不是那个插件：它只负责**关掉**与 prettier 冲突的
 * 排版类规则，避免两套东西互相打架，自己不报任何错。
 */
export default tseslint.config(
  js.configs.recommended,
  ...tseslint.configs.recommended,
  prettierConfig,
  {
    files: ['**/*.{ts,tsx}'],
    // 只注册插件、只开两条经典规则，**不用** `reactHooks.configs.flat`：
    // 后者会一并打开 `use-memo` / `immutability` / `static-components` 等一批 error 级新规则，
    // 对一个从没被 lint 过的仓库来说是一次性引爆几十条，没人会去逐条看。
    plugins: { 'react-hooks': reactHooks },
    rules: {
      'react-hooks/rules-of-hooks': 'error',
      'react-hooks/exhaustive-deps': 'warn',
      '@typescript-eslint/no-unused-vars': ['error', { argsIgnorePattern: '^_' }],
      '@typescript-eslint/consistent-type-imports': 'error',
      '@typescript-eslint/no-explicit-any': 'warn',
    },
  },
  {
    ignores: ['dist/**', 'build/**', 'node_modules/**'],
  },
);
