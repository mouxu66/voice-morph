# 应用自动更新

设置 → 应用 → **检查更新**；应用启动 12 秒后还会**静默检查一次**，有新版自动弹更新页（版本号 + 更新内容 + 下载进度 + 一键安装）。

## 设计

| 决策            | 选择                        | 原因                                                                 |
| ------------- | ------------------------- | ------------------------------------------------------------------ |
| 更新源           | 静态清单 `latest.json`        | GitHub Releases / 对象存储 / 网盘直链 / 局域网共享目录都能放，不绑定任何平台            |
| 实现方式          | 自写（Node 内置模块）              | 不引 `electron-updater`，不用联网装包、不用签名工具链                                |
| 默认行为          | **未配置更新源 = 完全离线**          | 守住项目"纯本地"红线：不填地址就一个网络请求都不发                                         |
| 完整性           | sha256 必填校验                | 清单没给 sha256 直接拒绝安装，防下载损坏/劫持                                        |
| 用户数据          | 更新不碰数据                    | 素材与输出在 `userData` 或 `D:\变声\media`，不在安装目录，覆盖安装不丢东西                   |

## 清单格式（`latest.json`）

```json
{
  "version": "0.3.0",
  "notes": "### 新增\n- 自动音高建议\n- 降噪强度三档\n\n### 修复\n- 降噪把弱人声压断",
  "pub_date": "2026-09-02T22:00:00Z",
  "url": "https://example.com/voicemorph/download/变声工坊-0.3.0-Setup.exe",
  "sha256": "小写 64 位十六进制",
  "size": 123456789,
  "mandatory": false
}
```

- `notes`：更新页正文，支持 `###` 小标题、`- ` 列表、空行分段（纯文本增强，不引 markdown 库）。
- `url`：支持 http/https，自动跟随重定向（GitHub 资源地址会 302 到 CDN）。
- `mandatory`：为 `true` 时不给"跳过此版本"。

## 启用（分发方做一次）

编辑 `web/electron/updater.cjs` 顶部的常量：

```js
const DEFAULT_MANIFEST_URL = "https://example.com/voicemorph/download/latest.json";
```

留空即保持纯本地。运行时可用环境变量 `VM_UPDATE_URL` 覆盖，方便调试或内网分发时临时改指向。

## 发布一个版本

```powershell
# 1) 改版本号（web/package.json 的 version 字段）
# 2) 打包
cd D:\变声\web
npm run electron:build

# 3) 生成清单（自动算 sha256 / 体积 / 版本号）
node web\electron\make-update-manifest.cjs --dir release2 --base-url https://example.com/voicemorph/download --notes docs\whats-new\0.3.0.md

# 4) 把 release2 里的安装包 + latest.json 一起上传到 --base-url 指向的位置
```

## 行为细节

- 静默检查在启动 12 秒后跑，失败全程静默，绝不打扰正常使用。
- 「跳过此版本」记在 `userData/update-skip.json`，之后不再自动弹；手动点"检查更新"仍能看到。
- 下载落在 `userData/updates/`，同名文件已存在且校验过则直接复用，不重复下载。
- 点「立即更新」：detached 拉起 NSIS 安装包（脱离主进程，应用退出后仍存活），0.8 秒后应用退出让安装程序覆盖文件。
- 非桌面端（网页 / Vite / 局域网访问）没有 `window.electron`，设置里不显示"检查更新"入口，前端自动降级。

## 已知限制

- 只支持 Windows NSIS 安装包（`.exe`）。
- 不做增量更新，每次下载完整安装包。
- 不做代码签名校验（安装包本身的 SmartScreen 提示仍在，与更新机制无关）。
- 打包分发含 GPL-3.0 组件（Seed-VC），公开分发前需自行评估合规（见 `docs/ROADMAP.md` 的 P1-2）。
