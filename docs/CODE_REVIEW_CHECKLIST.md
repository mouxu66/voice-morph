# 作者自审清单（Pre-Review Checklist）

> 提交前逐条过一遍，能自检的当场修。对应 `CODE_REVIEW_GUIDE.md` 与 `CODE_REVIEW_PROCESS.md`。

## 一、通用（必过）
- [ ] 改动只解决一个清晰意图，commit 信息用 conventional 格式（`feat/fix/docs/...`）。
- [ ] 无 `.env` / 密钥 / GitHub `gho_` 令牌入库；个人绝对路径（`D:/...mouxu...`）未写死。
- [ ] 无静默吞异常（`except Exception: pass` / 只 `continue` 不记日志）。
- [ ] 异常捕获尽量窄，关键路径失败有明确错误/日志/降级。
- [ ] 不带 `eval` / `exec` / `pickle.loads` / `yaml.load`（yaml 用 `safe_load`）。
- [ ] 用户输入有校验；文件路径有白名单/.realpath 防穿越。
- [ ] 共享可变状态（`global` / `STATE` / 模型句柄）有锁保护。

## 二、后端 `m2_server`
- [ ] `npm`/pytest 前设 `CODEBUDDY_SAFE_DELETE_ENABLED=0`（否则清理类测试随机失败）。
- [ ] 后端全量回归通过：
  ```bash
  CODEBUDDY_SAFE_DELETE_ENABLED=0 .venv/Scripts/python.exe -m pytest m2_server -q
  ```
- [ ] 新增/改动行为在 `m2_server/tests/` 有对应用例（铁律 2）。
- [ ] 日志用 `logging` 而非 `print`（桌面端子进程 print 输出丢失）。
- [ ] 网络/下载（market_*）带超时、重试、路径白名单。

## 三、推理 / RVC / TTS 流式
- [ ] `load_vc` / TTS model 复用单例，不重复加载；warmup 在 serve 前完成。
- [ ] 流式 `generate_voice_clone_streaming` 正确逐块 yield；客户端断开能取消。
- [ ] 加载/预热失败有降级（如 TTS 直出）并记状态。

## 四、前端 `web`
- [ ] 改 TS 后跑 `cd web && npm run build`（`tsc -b && vite build`）通过，无遗留 `any` 绕过。
- [ ] 图片/音频 `src` 过 `mediaUrl()`。
- [ ] 报错走 `notify.tsx` + `friendlyError()`。

## 五、Electron 主进程
- [ ] 不依赖 `ELECTRON_RUN_AS_NODE` / `NODE_OPTIONS` 注入；IPC handler 内异常有 catch。
- [ ] 不动 `.asar_tmp/`（旧 asar 解包残留）；重打包只走 `cd web && npm run electron:build`（`repack_asar.cjs` 等 4 个旧脚本已于 2026-09-13 删除）。

## 六、收尾
- [ ] 破坏性清理走 `tools/hard_delete.py`，不依赖 `shutil.rmtree` 真删。
- [ ] 踩坑/决策已写入 `D:\变声\.workbuddy\memory\YYYY-MM-DD.md`（如适用）。
- [ ] PR 描述包含：改了什么、怎么验证、测试结果/截图、遗留 🟡。
