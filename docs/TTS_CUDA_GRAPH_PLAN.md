# Qwen3-TTS 推理加速：CUDA Graph + StaticCache 实施方案

目标：让「语音 → ASR → 文字 → TTS」级联链路达到实时（RTF ≥ 1.0），
从而用「文字中转」彻底消除源说话人口音（RVC 直接转换做不到这一点）。

## 一、现状基线（实测，2026-08-30）

| 环节 | 耗时 | 实时率 |
|---|---|---|
| ASR（faster-whisper small @ CUDA） | 0.53s / 6s 音频 | **11.3x** ✅ 不是瓶颈 |
| TTS（Qwen3-TTS 1.7B） | 13~20s / 6s 音频 | **0.35~0.48x** ❌ 唯一瓶颈 |

同一负载反复跑波动达 ±30%，**小于 25% 的差异均视为噪声**。

### 瓶颈定性

```
worker 进程 CPU 97%（单核跑满）   GPU 20%   显存恒定 4562MB
py-spy 抓栈：transformers/generation/utils.py:2564 generate → :2787 _sample
```

即：**逐步自回归的 Python 开销压过了 GPU 计算**，不是算力/显存瓶颈。

### 已排除（实测无效或有害）

| 手段 | 结果 |
|---|---|
| `do_sample=False`（贪心） | 无显著差异 |
| `use_cache=True` | 无显著差异 |
| x-vector 声纹模式 vs ICL | 无显著差异 |
| `OMP_NUM_THREADS=1` | **慢 13%** |
| `OMP_NUM_THREADS=12` | **慢 25%** |
| `TTS_ATTN=sdpa` 环境变量 | 不存在，无效（真要设得传 `attn_implementation`） |
| llama.cpp / GGUF | 其公开数据 RTF 0.35，与现状持平，无收益 |
| Continuous Batching | 优化的是吞吐量，我们单请求无并发，场景不匹配 |

## 二、架构事实（决定方案可行性）

来源：`tts_models/qwen3-tts-1.7b-base/config.json`

| | Talker（主） | Code Predictor（副） |
|---|---|---|
| `num_hidden_layers` | 28 | 5 |
| `hidden_size` | 2048 | 1024 |
| `num_key_value_heads` | 8 | 8 |
| `head_dim` | 128 | 128 |
| `vocab_size` | 3072 | 2048 |
| `num_code_groups` | 16 | 16 |

### 三个关键结论

**1. 每帧是固定 2 次 forward，没有变长内层循环 —— 这是 CUDA Graph 可行的前提。**

Code predictor 有 `num_code_groups - 1 = 15` 个并行 LM head
（`modeling_qwen3_tts.py:1168` 的 `nn.ModuleList`），
**一次 forward 同时吐出 15 个 codebook**。因此：

```
每帧 = 1 次 talker forward(1 token) + 1 次 code predictor forward
```

步数固定 → 形状固定 → 可安全捕获为 CUDA Graph。

**2. 步数规模**

12 Hz tokenizer → 6.3s 音频 ≈ 76 帧 → **约 152 次 forward**。
实测 13~20s，即每次 forward 约 **85~130ms**。
而 1.7B 模型单 token forward 在该卡上本应 ~15~25ms，**存在 4~6x 水分** —— 正是要挤掉的部分。

**3. 白收的开销：`output_hidden_states=True` 被强制开启**

```python
# modeling_qwen3_tts.py:2064
"output_hidden_states": getattr(kwargs, "output_hidden_states", True),
# modeling_qwen3_tts.py:2280  —— 只用了最后一个
talker_codes = torch.stack([hid[-1] for hid in talker_result.hidden_states ...], dim=1)
```

每步收集 **29 个** hidden state（28 层 + 最终 norm），但只取 `hid[-1]`。
这些 Python 层 tuple 拼接收集 + `return_dict_in_generate` 簿记是纯浪费。

**自定义 decode 循环天然绕开这一点** —— 直接调模型、只取最终 hidden state，
这部分开销不需要 CUDA Graph 就能消掉。收益可能独立于 CUDA Graph 之外。

## 三、显存估算

**当前**：已用 4562 MB / 8151 MB，**空闲约 3589 MB**。

**新增开销**：

| 项目 | 计算 | 大小 |
|---|---|---|
| Talker KV cache | 28 层 × 2(K,V) × 8 heads × 128 dim × 2B = 112 KB/token；× 2048 tokens | 229 MB |
| Code predictor KV cache | 5 × 2 × 8 × 128 × 2B = 20 KB/token；× 2048 tokens | 41 MB |
| CUDA Graph（talker decode） | 28 层 batch=1/seq=1 峰值激活，常驻 | ~80 MB |
| CUDA Graph（code predictor） | 5 层 | ~20 MB |
| 静态输入输出缓冲 | 每图数十 KB | < 5 MB |
| **合计** | | **约 375 MB** |

> `max_cache_len=2048` 的取法：12Hz × 60s 生成 = 720 帧，
> 再加最长 20s 参考音频（240 帧）与文本 token，2048 有充足余量。

**结论：显存不是约束。** 空闲 3589 MB，新增约 375 MB，
即使估算偏差 3 倍仍绰绰有余。真正的工作量在代码，不在显存。

## 四、实验设计（分阶段 go/no-go）

### Phase 0 · 插桩测量 —— 拿到理论上限（约 30 分钟）

在 `self.talker.generate()` 外围打点，统计：

- prefill 步数与耗时（变长，不进 graph）
- decode 帧数、每帧耗时
- **talker forward 与 code predictor forward 各自的耗时占比**
- 单步 forward 的 eager 基线耗时

产出：每步耗时基线 T_step，以及理论最优 = prefill + frames × T_step_min。
**没有这个数字，后面无法判断优化是否到位。**

### Phase 1a · 自定义 decode 循环（不用 CUDA Graph）—— 性价比最高的一步（2~3 小时）

先**不动** CUDA Graph，只把 HF 的 `generate()` 换成手写循环：

- 直接调 `talker.model.forward()`，绕过 `generate` 的 logits processor / warper / 
  逐步簿记，只取最终 hidden state
- 保留 DynamicCache，不引入 StaticCache
- prefill 一次，然后逐帧 decode

判据：
- 若提速 ≥ 2x → 已经很可能达成目标，CUDA Graph 变成可选加分项
- 若提速 < 1.5x → 开销不在 generate 簿记，直接进入 Phase 1b

**这一步风险最低、代码改动最小，且是 Phase 2 的必要前置**（反正都要写自定义循环）。

### Phase 1b · 单步 CUDA Graph PoC —— go/no-go 关卡（2~3 小时）

独立脚本，完全不碰业务代码：

```python
# 伪代码
# 1. 构造一次 decode step（talker 单 token forward + code predictor forward）
# 2. 先测 eager 耗时 T_eager（多轮取中位数）
# 3. warmup 若干步后捕获：
g = torch.cuda.CUDAGraph()
with torch.cuda.graph(g):
    out = talker_decode_step(...)      # 形状全部固定
# 4. 测 replay 耗时 T_graph
```

判据：
- `T_eager / T_graph ≥ 2.0` → **继续**，方案二成立
- `1.5 ~ 2.0` → 值得做，但要调低预期
- `< 1.5` → **放弃方案二**，改用「非实时级联」（方案 A），别浪费两天

### Phase 2 · 完整集成（4~8 小时）

- `DynamicCache` → `StaticCache(max_cache_len=2048)`
- **prefill 走 eager**（变长，不可入图），**decode 走 graph**（定长）
- 捕获 2 张图：talker decode step、code predictor step
- **EOS 检测必须放在 graph 外**（控制流不能入图）：每步 replay 后同步判断，
  命中则跳出
- 保留 `trailing_text_hidden` / `tts_pad_embed` 等自定义入参（这两个是
  Qwen3-TTS 特有的，HF 标准循环没有）

### Phase 3 · 验证（1~2 小时）

- **音质**：同一参考音频 + 同一文本，对比优化前后输出，
  用现成的 `/emb` 端点算声纹余弦相似度（应 > 0.95），并人工盲听
- **速度**：同一负载跑 5 轮取**中位数**（务必注意 ±30% 波动）
- **显存**：`nvidia-smi` 采样峰值，与第三节估算对照
- **长音频**：测 30s / 60s 输入，确认 StaticCache 不越界

## 五、风险与兜底

| 风险 | 影响 | 兜底 |
|---|---|---|
| CUDA Graph 与 bf16 + Windows + torch 2.8 组合出问题 | 捕获失败 | Phase 1b 专门验证，失败即止损 |
| 自定义循环改写引入音质回归 | 声音变差 | Phase 3 声纹相似度 + 盲听双验；保留原路径可一键回退 |
| `trailing_text_hidden` / `tts_pad_embed` 在自定义循环中处理错 | 输出乱码 | 先做单句对照，逐张量比对中间结果 |
| 提速不够 3x，仍达不到实时 | 目标落空 | 退守「非实时级联」：录完等几秒再发，口音问题照样解决 |
| 改动深、后续 qwen_tts 升级会冲突 | 维护成本 | 封装在独立模块，不直接改 site-packages |

## 六、止损原则

Phase 1b 是唯一硬关卡。**若单步 CUDA Graph 加速比 < 1.5x，立即停止方案二。**

理由：目标 RTF 从 0.35~0.48 提到 ≥ 1.0 需要约 2.5x 整体提速，
而 CUDA Graph 只能覆盖 decode 步（prefill 不变），
所以单步加速比必须显著高于 2.5x 才有意义。
若单步只有 1.5x，最终整体大概率仍不达实时，此时应改走「非实时级联」，
把精力放回音色本身。
