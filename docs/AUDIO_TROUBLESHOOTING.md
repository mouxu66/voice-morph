# 音频故障排查手册（AI 可读）

> 用途：当出现"扬声器没声音 / 恢复音频无效 / 变声后电脑无声"等问题时，供 AI 或开发者按此文档快速精确定位。
> 最后更新：2026-08-27（真实案例：扬声器显示正常但完全无声，重启后恢复）

## 一、本机固定音频设备约定（永久有效，勿改）

| 角色 | 设备完整名 | 说明 |
|---|---|---|
| 系统扬声器 | `扬声器 (Senary Audio)` | 笔记本原生声卡，枚举顺序第 3 |
| 系统麦克风 | `麦克风阵列 (Senary Audio)` | 原生麦克风 |
| 变声虚拟声卡输出 | `CABLE Input (VB-Audio Virtual Cable)` | RVC 变声结果的写入端 |
| 变声虚拟声卡录音 | `CABLE Output (VB-Audio Virtual Cable)` | 微信/游戏从这里录到变声后的人声 |
| Steam 虚拟设备 | `扬声器/麦克风/内部 AUX (Steam Streaming ...)` | **永远不能设为默认设备**，枚举顺序靠前（第 1），是历史事故根源 |

铁律：任何 apply/restore/reset 兜底逻辑都绝不能把默认设备设到 Steam 设备上。判断时必须用「描述 + 提供方」完整名（如 `扬声器 (Senary Audio)`），只看描述"扬声器"无法区分同名设备。

## 二、变声功能的音频链路（rvc_live.py 实现）

```
麦克风阵列 (Senary Audio)
   ↓ 输入
RVC realtime_gui.py（D:\RVC\realtime_gui.py --auto-start）
   ↓ 实时推理输出
CABLE Input (VB-Audio Virtual Cable)          ← RVC 的输出设备
   ↓ 虚拟声卡内部转发
CABLE Output                                   ← 系统默认【录音】设备被切到这里
   ↓
微信/游戏/QQ 等（它们的"麦克风"= CABLE Output = 变声后的人声）
```

关键点：
- 一键开启（`POST /api/rvc/live/start`）：备份当前默认设备 → 录音默认切到 CABLE Output → 后台拉起 RVC 窗口；**系统播放（扬声器）不被改动**，所以正常情况下不会导致"电脑放音乐没声音"。
- 一键关闭/窗口关闭：restore 备份还原，失败再 reset 兜底（找非 CABLE/非 Steam 的真实设备，优先 Senary）。
- 相关文件：`m2_server/rvc_live.py`（编排）、`m2_server/audio_config.ps1`（设备切换与还原）、备份文件 `%LOCALAPPDATA%\rvc_audio_backup.txt`。
- **闭环实测记录（2026-08-27，通过）**：start → 录音三角色=CABLE Output、播放三角色=扬声器(Senary) 不动、RVC 进程存活；stop → restore=true，录音三角色回到麦克风阵列，备份文件删除。参照此基准判断回归。

## 三、故障树（按症状查）

### 症状 A：设置里默认设备全对，但扬声器完全无声
**2026-08-27 已发生并解决的真实案例。**

- 判定方法：运行 `tts_trial/audio_probe.py`（播音 + IAudioMeterInformation 测峰值），
  结果写 `tts_trial/probe_out.txt`：
  - `peak > 0.01` → 链路通，问题在别处（音量/应用级静音）
  - `peak = 0` 且耳机有声 → 扬声器端点卡死
- 根因：Senary 驱动处于「等待系统重新启动才能完成上一次操作」的挂起态
  （用管理员跑 `pnputil /restart-device <设备实例ID>` 可确诊，会报"设备正在等待系统重新启动"；
  此时 `Disable-PnpDevice` 报"常规故障"，`Enable-PnpDevice` 假成功）。
- 解法：**重启电脑**（唯一解）。重启后重跑 audio_probe.py 确认 peak > 0。
- 备注：插耳机有声说明驱动总栈活着，仅扬声器端点是"僵尸"，与音量/默认设备无关。

### 症状 B：变声开始后没声音 / 恢复音频无效
1. 先看默认设备被切到哪：`powershell -File m2_server/audio_config.ps1 -action status`
   （或 `-action diag` 看全量设备状态。state 含义：1=Active 2=Disabled 4=NotPresent）
2. 若播放默认变成了 Steam 设备 → 不应发生，检查 audio_config.ps1 是否为修复版
   （特征：包含 `FindReal` 且匹配 `Steam Streaming` 排除逻辑），并手动 reset：
   `POST /api/rvc/live/reset`
3. 若录音默认还在 CABLE Output 但 RVC 窗口已关 → 备份残留，调 `/api/rvc/live/reset`；
   服务器启动时会自动做同样的清理（见 rvc_live.py `_auto_clean()`）。
4. 若恢复后再遇症状 A（播放端点仍无声），转症状 A 流程。

### 症状 C：变声功能本身不可用
依次检查：
1. 主服务 8000 存活：`GET http://127.0.0.1:8000/api/health`（必须用 `D:\变声\.venv` 启动）
2. `GET /api/rvc/live/status` → model_ok（缺 `D:\RVC\logs\meituan_rat\meituan_rat.pth`+index 则要先训练）
3. CABLE 设备存在且 Active（diag 里 state=1）；VB-Cable 被禁用会导致 apply 找不到 CABLE Output
4. RVC 窗口秒退 → 看 `D:\RVC\logs\meituan_rat\realtime_gui.log`

## 四、给 AI 的运维工具箱

| 脚本 | 作用 | 结果落盘 |
|---|---|---|
| `tts_trial/audio_probe.py` | 播 440Hz 测试音 + 测默认扬声器峰值电平（判定"真有无声"的金标准） | `tts_trial/probe_out.txt` |
| `tts_trial/audio_diag.py` | 设备全量状态 + audio_config.ps1 diag + 服务状态 + 备份文件是否存在 | `tts_trial/diag_out.txt` |
| `tts_trial/endpoint_test.py` | 枚举全部端点 + 测试目标端点 WASAPI 共享模式初始化 | `tts_trial/endpoint_test.txt` |
| `tts_trial/audio_vol_check.py` | 主音量/静音 + 各会话音量 | `tts_trial/vol_out.txt` |

约定（重要）：
- 需要提权（UAC）的脚本**必须放在纯英文路径**（如 `C:\Users\mouxu\AppData\Local\Temp\`），
  中文路径会让 `Start-Process -Verb RunAs` 的参数乱码 → 提权进程静默失败（exit 0 无日志）。
- 终端长任务会被超时杀死：验证类脚本一律结果写文件 + `Start-Process` 分离运行。
