# tools/desktop-control · 桌面控制小工具

给 agent（或你自己）一对**眼睛 + 手**：截图看屏幕、按结构化控件定位并点击、输入文本。
只用 Windows 自带的 PowerShell 5.1 + UI Automation，**零第三方依赖**（不装 Py 包、不装 AutoHotkey）。

```bash
DC="powershell.exe -NoProfile -ExecutionPolicy Bypass -File tools/desktop-control/dc.ps1"
```

## 常用命令

```bash
# 0. 自检：确认本机允许操控（交互会话 / 分辨率 / 缩放 / 前台窗口）
$DC probe

# 1. 看一眼屏幕（-Preview 会额外出一张 1280 宽 JPEG，agent 只能读这张）
$DC shot -Preview
#    → tools/desktop-control/out/shot-<时间>.png （全分辨率，存档用）
#    → tools/desktop-control/out/shot-<时间>.jpg （~100KB，肉眼/agent 都看这张）

# 2. 现在有哪些窗口
$DC windows

# 3. 某个窗口里有哪些控件（含每个控件的矩形和支持的 UIA 模式）
$DC tree -Title "记事本" -Depth 4

# 4. 点它
$DC click -Title "记事本" -AutoId SaveButton      # 优先走 UIA Invoke/Toggle/Select
$DC click -Title "记事本" -Name "保存"             # 按控件名（先精确后包含）
$DC click -Title "记事本" -Type Button            # 按控件类型（名字不可用时的兜底）
$DC click -X 1200 -Y 800                         # 最后手段：裸坐标

# 5. 输入 / 按键
$DC type -Title "记事本" -Type Edit -Text "hello"  # 优先 ValuePattern（Unicode 安全）
$DC keys -Title "记事本" -Keys "^s"                # SendKeys 语法

# 6. 端到端自测（自带 WPF 靶子窗口，不碰你的任何窗口）
$DC selftest        # 退出码 0 = 全绿
```

## 为什么是 UI Automation 优先

按坐标点像素，界面一挪就全废；UIA 能拿到**控件的精确矩形和支持的动作**：

- `tree` 输出的 `pat` 字段 = 该控件支持的模式（`Invoke` / `Toggle` / `Value` / `SelectionItem` …）。
- `click` 只要能拿到 `Invoke`/`Toggle`/`SelectionItem`/`ExpandCollapse` 就**不模拟鼠标**，
  直接调 UIA 动作 —— 不受窗口遮挡、不需要窗口在前台、不会点偏。
- 只有当控件没有任何可用模式时，才退回"鼠标点控件矩形中心"。
- `type` 优先 `ValuePattern.SetValue`（绕开输入法、Unicode 直接进），
  WinForms 这类没有 ValuePattern 的控件退回"剪贴板粘贴"（会临时改写剪贴板，用完还原）。

## 参数里带中文怎么办

控制台代码页会把 argv 里的中文搞乱。凡是标题/文本含中文，**走 UTF-8 JSON 参数文件**：

```bash
cat > /tmp/args.json <<'EOF'
{ "Action": "type", "Title": "变声工坊", "Type": "Edit", "Text": "布菲写入的中文 ✅" }
EOF
$DC -Action type -ArgsFile /tmp/args.json
```

（`Action` 写在 JSON 里也算数；JSON 里的字段名 = 参数名。）

## 产物与 .gitignore

结果写到 `tools/desktop-control/out/`：

- `*.json` —— 每次动作的结果详情（窗口、命中元素、用的哪种模式、矩形）
- `*.png` —— 全分辨率截图
- `*.jpg` —— 降采样预览

`.gitignore` **只忽略 `*.png` 和 `*.json`，故意留下 `*.jpg`**：读取工具对被 ignore 的路径一律
`[BLOCKED]`，所以 agent 必须能读到那张 jpg，才算"看得见屏幕"。改动这条 ignore 前先想清楚。

> ⚠️ **隐私红线**：`out/*.jpg` 是**你桌面的真实截图**（可能含聊天记录、密钥窗口、别人的消息），
> 而本仓库是**公开**的。被 ignore 的 png/json 相对安全，**这个 jpg 不是**。
> 因此：① 绝不要 `git add -A`；② 用完就删（`rm -f tools/desktop-control/out/*`）；
> ③ 要长期留证的截图请先裁剪/模糊，别直接放进来。

## 已知边界（别指望它）

- **看不到没截图的东西**；每次"看"都是一次 `shot`，不是实时视频。
- **Chromium/Electron 窗口的 UIA 树近乎空壳**（只有 `Chrome Legacy Window` 之类的 Pane）：
  Freebuff、VS Code、以及自家 `变声工坊` 都属于这类。它们要么靠截图 + 坐标点，
  要么给应用加 `--force-renderer-accessibility` 才有 DOM 级 UIA。
  （`m2_server/wechat_uia.py` 能拿到微信结构化控件，是因为微信走 Qt accessibility gate，不是同一回事。）
- **WinForms 控件在 UIA 里基本是哑巴**（`ControlType=Pane`、无名字、无 ValuePattern）——
  所以自带的测试靶子 `sandbox` 用的是 **WPF**（原生 UIA provider）。
- **系统自带应用不能当测试靶子**：`Start-Process notepad` 在本机直接失败（Win11 记事本是
  Store 别名，当前安全上下文起不来）。要靶子就用 `$DC sandbox`。
- `keys` 走 SendKeys，对**注入键盘做了过滤**的应用（如微信语音快捷键）无效，那种要换鼠标路径。
- 不做提权：动不了以管理员身份运行的窗口。

## 自测

```bash
$DC selftest   # spawn WPF 沙箱 → UIA 找到输入框 → 走 type 动作写入 → 回读校验 → 走 click 关窗 → 确认消失
```

失败时退出码非 0，详情在 `out/selftest.json`。改动 `dc.ps1` 后**必须**跑一遍再看结果。

## 与 Python 路线的关系

本机 `.venv` 里**已经装了** `uiautomation 2.0.29` + Pillow，能力比这个 PowerShell 壳强
（`Click` / `SendKeys` / `ControlFromCursor` / `ControlFromHandle` / 正则查找 / `WalkTree`），
`m2_server/wechat_uia.py` 就是同路子。**要做常态化桌面自动化，优先扩 Python 那条**；
`dc.ps1` 的定位是"任何环境都能立刻跑"的兜底 + 不碰 Python 依赖的应急手。
