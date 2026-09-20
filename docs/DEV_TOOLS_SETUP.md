# 完整开发工具链配置

##  已安装的完整工具列表

### **AI 插件（用户级全局）**
- ✅ Context7 - AI 知识库，自动查文档
- ✅ Postman - API 管理和测试
- ⭐ 架构可视化 - 理解复杂系统架构（建议安装）
- ⭐ QMind 知识库 - 项目知识 RAG 问答（建议安装）

### **npm 包（前端）**
- ✅ @tanstack/react-query - 状态管理，替代轮询
- ✅ electron-store - 配置持久化
- ✅ electron-log - 结构化日志
- ✅ vitest@4 - 快速单元测试
- ✅ @testing-library/react - React 测试库
- ✅ @playwright/test - E2E 测试
- ✅ @sentry/electron + @sentry/react - 错误追踪
- ✅ prettier - 代码格式化（`npm run format`，**可选执行**，见下方 ⚠️）
- ✅ eslint + @eslint/js + typescript-eslint + eslint-plugin-react-hooks - 静态检查
- ✅ eslint-config-prettier - 只关掉与 prettier 冲突的规则，**不**把排版塞进 lint

### **Python 包（后端）**
- ✅ pydantic-settings - 环境变量管理
- ✅ structlog - 结构化日志
- ✅ pytest-asyncio - async 测试支持
- ✅ pytest-cov - 代码覆盖率
- ✅ pytest-xdist - 并行测试
- ✅ pytest-clarity - 更好的错误信息
- ✅ hypothesis - 属性基测试
- ✅ ruff - 超快 lint/formatter
- ✅ mypy - 类型检查
- ✅ black + isort - 代码格式化
- ✅ soundgraph - 音频可视化
- ✅ matplotlib - 绘图

---

## ⚙️ 配置文件

### **1. Prettier 配置**

`web/.prettierrc`（2026-09-20 按仓库**实测风格**修正：这里原来的 `semi: true` +
`singleQuote: true` 跟代码正好相反）：

```json
{
  "semi": false,
  "singleQuote": false,
  "tabWidth": 2,
  "trailingComma": "all",
  "printWidth": 100,
  "endOfLine": "auto"
}
```

> ⚠️ **`npm run format` 会改动约 88 个文件（-3444 / +7950 行）** —— 本仓库从来没被
> prettier 格式化过，实测 19678 行里 1476 行带分号（其中 1324 行是缩进的接口/类型成员，
> 手写风格如此），且 5% 的行超过 100 字符。所以格式化是**可选**动作，不是日常流程；
> 想跑就单独成一个提交，别混在功能改动里。

### **2. ESLint 配置**

**只有** `web/eslint.config.mjs`（flat config）。旧的 `web/.eslintrc.cjs` 已于
2026-09-20 删除 —— ESLint 10 不再读 eslintrc，留着它只会造成「看起来权威、实际 inert」
的双配置漂移（而且它 extend 的是 `recommended-requiring-type-checking`，比现役规则严得多）。

```javascript
import js from '@eslint/js';
import tseslint from 'typescript-eslint';
import prettierConfig from 'eslint-config-prettier';
import reactHooks from 'eslint-plugin-react-hooks';

export default tseslint.config(
  js.configs.recommended,
  ...tseslint.configs.recommended,
  prettierConfig,            // 只关掉冲突的排版规则，自己不报错
  {
    files: ['**/*.{ts,tsx}'],
    plugins: { 'react-hooks': reactHooks },
    rules: {
      'react-hooks/rules-of-hooks': 'error',
      'react-hooks/exhaustive-deps': 'warn',
      '@typescript-eslint/no-unused-vars': ['error', { argsIgnorePattern: '^_' }],
      '@typescript-eslint/consistent-type-imports': 'error',
      '@typescript-eslint/no-explicit-any': 'warn',
    },
  },
  { ignores: ['dist/**', 'build/**', 'node_modules/**'] },
);
```

> **为什么不用 `eslint-plugin-prettier`**：实测 `prettier/prettier: error` 会让 lint
> 报 3373 个错误，几乎全是 `Delete ';'`，**没有一个是真缺陷**；关掉后只剩 9 个真问题。
> 排版交给 prettier 单独跑（prettier 官方也不推荐这个插件）。
> 这条决定由 `web/src/lintConfig.test.ts` 钉住。

### **3. Vitest 配置**

创建 `vitest.config.ts`：

```typescript
import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  test: {
    globals: true,
    environment: 'jsdom',
    setupFiles: './test/setup.ts',
    include: ['src/**/*.{test,spec}.{js,mjs,cjs,ts,mts,cts,jsx,tsx}'],
    coverage: {
      reporter: ['text', 'json', 'html'],
      exclude: ['node_modules/', 'tests/'],
    },
  },
});
```

### **4. Playwright 配置**

运行 `npx playwright install --with-deps` 后，创建 `playwright.config.ts`：

```typescript
import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
  testDir: './e2e',
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  workers: process.env.CI ? 1 : undefined,
  reporter: 'html',
  use: {
    baseURL: 'http://localhost:5173',
    trace: 'on-first-retry',
  },
  projects: [
    { name: 'chromium', use: { ...devices['Desktop Chrome'] } },
    { name: 'firefox', use: { ...devices['Desktop Firefox'] } },
    { name: 'webkit', use: { ...devices['Desktop Safari'] } },
  ],
});
```

### **5. Python 配置**

创建 `pyproject.toml`：

```toml
[tool.ruff]
line-length = 100
target-version = "py311"
select = [
  "E",  # pycodestyle errors
  "W",  # pycodestyle warnings
  "F",  # Pyflakes
  "I",  # isort
  "B",  # flake8-bugbear
  "C4", # flake8-comprehensions
]
ignore = ["E501", "B008"]

[tool.black]
line-length = 100
target-version = ["py311"]

[tool.isort]
profile = "black"
line_length = 100

[tool.mypy]
python_version = "3.11"
warn_return_any = true
warn_unused_configs = true
disallow_untyped_defs = true
ignore_missing_imports = true

[tool.pytest.ini_options]
minversion = "8.0"
addopts = "-ra -q"
testpaths = ["m2_server/tests"]
asyncio_mode = "auto"
```

---

## 🚀 使用指南

### **前端开发流程**

1. **代码格式化**
   ```bash
   npm run format        # Prettier 格式化
   npm run lint          # ESLint 检查
   npm run lint:fix      # ESLint 自动修复
   ```

2. **单元测试**
   ```bash
   npm run test          # 运行所有测试
   npm run test:watch    # 监听模式
   npm run test:coverage # 生成覆盖率报告
   ```

3. **E2E 测试**
   ```bash
   npx playwright test   # 运行所有测试
   npx playwright test --ui  # UI 模式
   npx playwright show-trace  # 查看追踪
   ```

### **Python 开发流程**

1. **代码质量**
   ```bash
   ruff check m2_server/     # Lint 检查
   ruff format m2_server/    # 格式化
   black m2_server/          # 备用格式化
   isort m2_server/          # import 排序
   mypy m2_server/           # 类型检查
   ```

2. **测试**
   ```bash
   pytest m2_server/tests/              # 运行测试
   pytest -xvs                          # 详细输出
   pytest --cov=m2_server               # 覆盖率
   pytest -n auto                       # 并行测试
   pytest --clarity                     # 清晰错误信息
   ```

---

## 📊 工具对比

| 类别 | 旧方案 | 新方案 | 优势 |
|------|--------|--------|------|
| 状态管理 | 手动轮询 | React Query | 自动缓存、重试、后台更新 |
| 日志 | console.log | electron-log + structlog | 结构化、可查询、多级别 |
| 配置 | JSON 文件 | electron-store | 路径转换、自动序列化 |
| 测试 | 无 | Vitest + Playwright | 快速、专业、端到端 |
| 代码质量 | 部分 ESLint | Prettier + ESLint + TypeScript | 完整的质量保障 |
| 错误追踪 | 无 | Sentry | 实时捕获、性能监控 |
| Python Lint | ruff | ruff + mypy + black | 多层质量保障 |
| Python 测试 | pytest | pytest + xdist + hypothesis | 并行、高级测试 |

---

## 🎯 下一步

1. **配置 Git Hooks**
   ```bash
   python tools/install_hooks.py
   ```

2. **初始化测试框架**
   ```bash
   # 前端
   mkdir -p web/src/__tests__
   touch web/test/setup.ts
   
   # 后端
   mkdir -p m2_server/tests/__snapshots__
   ```

3. **配置 CI/CD**
   - GitHub Actions
   - 自动化测试
   - 代码质量检查

---

**恭喜你！现在你拥有了一套专业的、完整的开发工具链！** 🎉
