# 应用自动更新

设置 → 应用 → **检查更新**；应用启动 12 秒后还会**静默检查一次**，有新版自动弹更新页（版本号 + 更新内容 + 下载进度 + 一键安装）。

## 设计

| 决策            | 选择                        | 原因                                                                 |
| ------------- | ------------------------- | ------------------------------------------------------------------ |
| 更新源           | 静态清单 `latest.json`        | GitHub Releases / 对象存储 / 网盘直链 / 局域网共享目录都能放，不绑定任何平台            |
| 实现方式          | 自写（Node 内置模块）              | 不引 `electron-updater`，不用联网装包、不用签名工具链                                |
| 默认行为          | **默认走 GitHub Releases，可一键关掉** | 安装版开箱即能收到更新；要离线的机器把 `VM_UPDATE_URL=off` 即可回到"一个请求都不发" |
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

## 更新源

`web/electron/updater.cjs` 里的 `DEFAULT_MANIFEST_URL` **默认已指向本仓库的 GitHub Releases**：

```
https://github.com/mouxu66/voice-morph/releases/latest/download/latest.json
```

`/releases/latest/download/<asset>` 是 GitHub 的**永久别名** —— 永远解析到最新那个 Release 的同名
asset。所以发新版只要把 `latest.json` 与新安装包挂到新 Release 上，客户端不用改任何配置。

覆盖方式（按优先级）：

| 环境变量 | 效果 |
|---|---|
| `VM_UPDATE_URL=https://.../latest.json` | 换成自建源（对象存储 / 网盘直链 / 局域网共享都行） |
| `VM_UPDATE_URL=off`（也接受 `0` / `none` / `false` / `disabled`） | **关闭更新**，纯本地，一个网络请求都不发 |
| 不设 | 走上面的默认 GitHub 源 |

## 发布一个版本

```powershell
# 1) 一条命令：打包 + 生成清单 + 发 GitHub Release（自动 +1 版本号）
cd D:\变声
powershell -ExecutionPolicy Bypass -File scripts\release.ps1 -Publish -Notes "修复录音卡顿"

# 2) 完事。客户端下次启动 12 秒后静默检查即可看到更新
```

`-Publish` 会自动：建 tag `v<新版本>` → 建 Release（标记 latest）→ 上传 exe / blockmap / latest.json
→ 实测 `/releases/latest/download/latest.json` 能不能拿到正确版本。完整参数与排错见 `docs/release-sop.md`。

不用 `-Publish` 时仍走老路子：脚本打印「待上传文件」，你自己传到 `-BaseUrl` 指向的位置。

## 行为细节

- 静默检查在启动 12 秒后跑，失败全程静默，绝不打扰正常使用。
- 「跳过此版本」记在 `userData/update-skip.json`，之后不再自动弹；手动点"检查更新"仍能看到。
- 下载落在 `userData/updates/`，同名文件已存在且校验过则直接复用，不重复下载。
- **缓存包用完即清**：点过「立即更新」后写一个"待安装"标记，下一次启动时把 `updates/` 里的
  安装包删掉（约 100MB）。没点过更新的下载包不动，仍可复用缓存。之所以不在安装当时删 ——
  安装包被 NSIS 进程占用，Windows 不允许删。
- 点「立即更新」：detached 拉起 NSIS 安装包（脱离主进程，应用退出后仍存活），0.8 秒后应用退出让安装程序覆盖文件。
  安装是**原地替换**（按 appId 卸旧装新到同一目录），不会并存两份 —— 除非你在向导里手动改了安装目录。
- 非桌面端（网页 / Vite / 局域网访问）没有 `window.electron`，设置里不显示"检查更新"入口，前端自动降级。

## 已知限制

- 只支持 Windows NSIS 安装包（`.exe`）。
- 不做增量更新，每次下载完整安装包。
- 不做代码签名校验（安装包本身的 SmartScreen 提示仍在，与更新机制无关）。
- GPL-3.0 的 Seed-VC 链路**不在发行物里**：`seed_vc_repo/` 与 `seed_vc/` 均被 `.gitignore` 忽略
  （入库文件数 0），且 `extraResources` 只收 `dist` / `m2_server` / `tools` / `requirements.txt`。
  所以公开分发这个安装包不带该组件；只有本机自己启用该链路时才涉及它的许可（2026-09-14 核实）。
