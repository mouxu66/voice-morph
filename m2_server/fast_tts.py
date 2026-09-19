"""fast_tts — Qwen3-TTS 克隆合成 CUDA Graph 加速（独立模块，不改 site-packages）。

背景（2026-08-30 Phase 0/1 实测，RTX 5060 Laptop 8G）：
    原版 generate 逐帧自回归，Python/内核启动开销占主导（worker 单核 97%、GPU 20%），
    6s 音频要 13~20s（RTF 0.35~0.48）。Phase 1b PoC：把「predictor prefill+14 步 decode
    + talker 1 步」整帧捕获为单张 CUDA Graph，36.9ms/帧，加速 4.97x，RTF≈2.3。

实现：
    1. prefill 复用官方预处理：临时把 talker.generate 换成抛异常的桩，调
       model.generate_voice_clone 截获 inputs_embeds / attention_mask /
       trailing_text_hidden / tts_pad_embed 以及合并后的采样参数（不复制上游逻辑，
       升级 qwen_tts 也不受影响）
    2. prefill 走 eager（变长），写入 StaticCache(max_cache_len)
    3. decode 每帧 replay 一张整帧 CUDA Graph（形状固定）；EOS / 循环控制流在图外
    4. 图参数分两类：
       - 静态输入缓冲：temperature / subtalker_temperature / repetition_penalty /
         eos_allowed / suppress 掩码 —— replay 前覆写，值变化不用重捕图
       - 烤进图的常量：do_sample / top_k / subtalker_dosample / subtalker_top_k
         （topk 的 k 必须是编译期 int）—— 按组合缓存图，LRU 上限 max_graphs
    5. 与原版的逐位差异仅来自 bf16 数值噪声（StaticCache 实体化 mask 走了不同的
       attention kernel 路径，PoC 实测 graph 与自定义 eager 循环逐位一致），
       音质由声纹相似度 + 盲听兜底（Phase 3）

不支持的场景抛 FallbackToSlow，调用方回退原版 generate_voice_clone：
    batch>1、top_p<1.0、temperature<=0、prefill 超过 max_cache_len、图捕获失败。

注意（教训）：推理模式必须持有上下文管理器引用再 __enter__，写成
`torch.inference_mode().__enter__()` 会在语句结束析构时立刻退出模式，
autograd 记录 StaticCache 的 in-place index_copy_，图永久累积直接爆显存。
"""

import gc
import traceback
from collections import OrderedDict

import torch
from transformers import StaticCache

NEG_INF = float("-inf")


class FallbackToSlow(Exception):
    """fast path 无法处理该请求，调用方应回退原版 generate_voice_clone。"""


class _Captured(Exception):
    """talker.generate 异常桩：携带真实入参跳出上游调用栈。"""

    def __init__(self, kwargs):
        super().__init__("prefill captured")
        self.kwargs = kwargs


class FastVoiceCloneEngine:
    """单例引擎：持有静态缓冲 + StaticCache + 若干整帧 CUDA Graph。

    生命周期由调用方（qwen3_tts_service）管理：换驻留模型前必须丢弃引擎引用并
    gc+empty_cache（图与缓存会钉住模型权重，否则旧模型释放不掉）。
    """

    def __init__(self, model, max_cache_len=2048, max_frames=1024, max_graphs=4):
        self.model = model
        inner = model.model
        self.talker = inner.talker
        self.predictor = self.talker.code_predictor
        self.tconfig = self.talker.config
        self.pconfig = self.predictor.config
        self.dev = self.talker.device
        self.vocab = int(self.tconfig.vocab_size)
        self.default_eos = int(self.tconfig.codec_eos_token_id)
        self.ng = int(self.tconfig.num_code_groups)
        self.max_cache_len = max_cache_len
        self.max_frames = max_frames
        self.max_graphs = max_graphs

        d, H = self.dev, int(self.tconfig.hidden_size)
        # ---- 图输入静态缓冲（replay 前覆写；跨请求复用） ----
        self.tok_in = torch.zeros(1, 1, dtype=torch.long, device=d)
        self.past_hidden_in = torch.zeros(1, 1, H, dtype=torch.bfloat16, device=d)
        self.text_embed_in = torch.zeros(1, 1, H, dtype=torch.bfloat16, device=d)
        self.t_cp_in = torch.zeros(1, dtype=torch.long, device=d)
        self.eos_allowed = torch.zeros((), dtype=torch.bool, device=d)
        self.hist_len_in = torch.zeros((), dtype=torch.long, device=d)
        self.hist = torch.zeros(max_frames + 2, dtype=torch.long, device=d)
        self.temperature_in = torch.ones((), dtype=torch.float32, device=d)
        self.sub_temperature_in = torch.ones((), dtype=torch.float32, device=d)
        self.rp_in = torch.ones((), dtype=torch.float32, device=d)
        # ---- 图输出 ----
        self.codes_out = torch.zeros(1, self.ng, dtype=torch.long, device=d)
        # ---- 掩码缓冲（值随请求覆写，replay 读当前值） ----
        self.suppress_mask = torch.zeros(self.vocab, dtype=torch.bool, device=d)
        self.eos_only_mask = torch.zeros(self.vocab, dtype=torch.bool, device=d)
        self._set_masks(self.default_eos, None)
        # ---- KV cache（全部图共享同一份缓冲） ----
        self.talker_cache = StaticCache(config=self.tconfig, max_cache_len=max_cache_len)
        self.pred_cache = StaticCache(config=self.pconfig, max_cache_len=self.ng + 1)
        # ---- predictor 位置常量 ----
        self._cp_prefill = torch.arange(2, device=d)
        self._cp_decode = [torch.tensor([p], device=d) for p in range(2, self.ng + 1)]
        self._hist_pos = torch.arange(max_frames + 2, device=d)

        self._graphs = OrderedDict()
        self.last_codes = None  # 调试：最近一次请求的 codes (n, NG)
        self.stats = {"requests": 0, "fallbacks": 0, "graph_rebuilds": 0}

    # ------------------------------------------------------------------
    # 参数掩码
    # ------------------------------------------------------------------
    def _set_masks(self, eos_id, suppress_tokens):
        sm = torch.zeros(self.vocab, dtype=torch.bool, device=self.dev)
        if suppress_tokens:
            ids = torch.tensor([int(t) for t in suppress_tokens], device=self.dev)
            sm[ids.clamp(0, self.vocab - 1)] = True
        sm[int(eos_id)] = False
        self.suppress_mask.copy_(sm)
        em = torch.zeros(self.vocab, dtype=torch.bool, device=self.dev)
        em[int(eos_id)] = True
        self.eos_only_mask.copy_(em)

    # ------------------------------------------------------------------
    # 采样（可整段捕获进图）
    # ------------------------------------------------------------------
    def _sample_pred(self, logits, do_sample, top_k):
        if not do_sample:
            return logits.argmax(-1, keepdim=True)
        logits = logits.float() / self.sub_temperature_in
        if 0 < top_k < logits.shape[-1]:
            v, idx = torch.topk(logits, top_k, dim=-1)
            return idx.gather(-1, torch.multinomial(torch.softmax(v, -1), 1))
        return torch.multinomial(torch.softmax(logits, -1), 1)

    def _sample_talker(self, logits, do_sample, top_k):
        if not do_sample:
            return logits.argmax(-1, keepdim=True)
        logits = logits / self.temperature_in
        if 0 < top_k < logits.shape[-1]:
            v, idx = torch.topk(logits, top_k, dim=-1)
            return idx.gather(-1, torch.multinomial(torch.softmax(v, -1), 1))
        return torch.multinomial(torch.softmax(logits, -1), 1)

    def _rep_penalty(self, logits):
        """对已生成 token 施加重复惩罚（rp_in==1.0 时数值上完全无操作）。

        hist 未写满的槽位由 valid 掩码排除；同一 token 多槽位 scatter 写回的值
        相同（只依赖该 token 自己的 logit），冲突结果确定。
        """
        idx = self.hist.view(1, -1)
        gathered = logits.gather(1, idx)
        penalized = torch.where(gathered < 0, gathered / self.rp_in, gathered * self.rp_in)
        valid = (self._hist_pos < self.hist_len_in).view(1, -1)
        return logits.scatter(1, idx, torch.where(valid, penalized, gathered))

    # ------------------------------------------------------------------
    # 整帧 body：predictor prefill(2) + 14 步 decode + talker 1 步 + 采样
    # ------------------------------------------------------------------
    def _frame_body(self, do_sample, top_k, sub_do_sample, sub_top_k):
        tok = self.tok_in
        last_id_hidden = self.talker.model.codec_embedding(tok)  # (1,1,H)
        pin = torch.cat((self.past_hidden_in, last_id_hidden), dim=1)  # (1,2,H)
        out = self.predictor.forward(
            inputs_embeds=pin,
            past_key_values=self.pred_cache,
            use_cache=True,
            cache_position=self._cp_prefill,
            generation_steps=0,
            output_attentions=False,
            output_hidden_states=False,
        )
        c = [tok, self._sample_pred(out.logits[:, -1, :], sub_do_sample, sub_top_k)]
        for i in range(1, self.ng - 1):  # 14 步 decode
            out = self.predictor.forward(
                input_ids=c[i],
                generation_steps=i,
                past_key_values=self.pred_cache,
                use_cache=True,
                cache_position=self._cp_decode[i - 1],
                output_attentions=False,
                output_hidden_states=False,
            )
            c.append(self._sample_pred(out.logits[:, -1, :], sub_do_sample, sub_top_k))
        codes = torch.cat(c, dim=-1)  # (1,NG)
        codec_hiddens = torch.cat(
            [last_id_hidden]
            + [
                self.predictor.model.codec_embedding[j](codes[:, j + 1 : j + 2])
                for j in range(self.ng - 1)
            ],
            dim=1,
        )
        frame_emb = codec_hiddens.sum(dim=1, keepdim=True) + self.text_embed_in
        out = self.talker.model(
            inputs_embeds=frame_emb,
            past_key_values=self.talker_cache,
            use_cache=True,
            cache_position=self.t_cp_in,
            output_attentions=False,
            output_hidden_states=False,
        )
        h = out.last_hidden_state  # (1,1,H)
        logits = self.talker.codec_head(h)[:, 0, :].float()  # (1,V)
        logits = self._rep_penalty(logits)  # HF 顺序：先惩罚
        logits = logits.masked_fill(self.suppress_mask, NEG_INF)
        logits = logits.masked_fill(self.eos_only_mask & ~self.eos_allowed, NEG_INF)
        next_tok = self._sample_talker(logits, do_sample, top_k)
        self.hist.index_copy_(0, self.hist_len_in.view(1), next_tok.view(1))
        self.codes_out.copy_(codes)
        self.tok_in.copy_(next_tok)
        self.past_hidden_in.copy_(h)

    def _get_graph(self, key, do_sample, top_k, sub_do_sample, sub_top_k):
        graph = self._graphs.get(key)
        if graph is not None:
            self._graphs.move_to_end(key)
            return graph
        while len(self._graphs) >= self.max_graphs:
            _, old = self._graphs.popitem(last=False)
            del old
            gc.collect()
            torch.cuda.empty_cache()
        try:
            self.stats["graph_rebuilds"] += 1
            s = torch.cuda.Stream()
            s.wait_stream(torch.cuda.current_stream())
            with torch.cuda.stream(s):
                for _ in range(3):
                    self._frame_body(do_sample, top_k, sub_do_sample, sub_top_k)
            torch.cuda.current_stream().wait_stream(s)
            torch.cuda.synchronize()
            graph = torch.cuda.CUDAGraph()
            with torch.cuda.graph(graph):
                self._frame_body(do_sample, top_k, sub_do_sample, sub_top_k)
        except Exception as exc:
            traceback.print_exc()
            self._graphs.clear()
            gc.collect()
            torch.cuda.empty_cache()
            raise FallbackToSlow(f"CUDA Graph 捕获失败: {exc}") from exc
        self._graphs[key] = graph
        return graph

    # ------------------------------------------------------------------
    # prefill 入参捕获（复用官方预处理）
    # ------------------------------------------------------------------
    def _capture_prefill(self, text, language, voice_clone_prompt, gen_kwargs):
        talker = self.talker
        orig_generate = talker.generate

        def _stub(*_a, **kw):
            raise _Captured(kw)

        talker.generate = _stub
        try:
            self.model.generate_voice_clone(
                text=[text],
                language=[language],
                voice_clone_prompt=voice_clone_prompt,
                **gen_kwargs,
            )
            raise FallbackToSlow("prefill 捕获桩未被触发")
        except _Captured as cap:
            kw = cap.kwargs
        finally:
            talker.generate = orig_generate
        return kw

    @staticmethod
    def _ref_code_of(prompt):
        """ICL 模式取参考 codes（x-vector 模式返回 None）。prompt 兼容 list/dict。"""
        if prompt is None:
            return None
        if isinstance(prompt, dict):
            rc = prompt.get("ref_code")
            icl = prompt.get("icl_mode")
            rc0 = rc[0] if isinstance(rc, (list, tuple)) and len(rc) else rc
            icl0 = icl[0] if isinstance(icl, (list, tuple)) and len(icl) else icl
        else:
            it = prompt[0] if isinstance(prompt, (list, tuple)) else prompt
            rc0 = getattr(it, "ref_code", None)
            icl0 = getattr(it, "icl_mode", False)
        return rc0 if icl0 else None

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------
    def generate(self, text, language, voice_clone_prompt, **gen_kwargs):
        """单条克隆合成，返回 (wavs, sr)。不支持场景抛 FallbackToSlow。"""
        texts = list(text) if isinstance(text, (list, tuple)) else [text]
        langs = list(language) if isinstance(language, (list, tuple)) else [language]
        if len(texts) != 1:
            raise FallbackToSlow(f"fast path 只支持 batch=1，收到 {len(texts)}")
        self.stats["requests"] += 1

        im = torch.inference_mode()
        im.__enter__()
        try:
            kw = self._capture_prefill(texts[0], langs[0], voice_clone_prompt, gen_kwargs)
            embeds = kw["inputs_embeds"]
            mask = kw["attention_mask"]
            trailing = kw["trailing_text_hidden"]
            pad = kw["tts_pad_embed"]

            do_sample = bool(kw.get("do_sample", True))
            top_p = float(kw.get("top_p") or 1.0)
            top_k = int(kw.get("top_k") or 0)
            temperature = float(kw.get("temperature") or 0.9)
            rp = float(kw.get("repetition_penalty") or 1.0)
            sub_do = bool(kw.get("subtalker_dosample", True))
            sub_top_p = float(kw.get("subtalker_top_p") or 1.0)
            sub_top_k = int(kw.get("subtalker_top_k") or 0)
            sub_temp = float(kw.get("subtalker_temperature") or 0.9)
            max_new = int(kw.get("max_new_tokens") or 2048)
            min_new = max(int(kw.get("min_new_tokens") or 2), 0)
            eos_id = int(kw.get("eos_token_id") or self.default_eos)
            suppress = kw.get("suppress_tokens")

            if top_p < 1.0 or sub_top_p < 1.0:
                raise FallbackToSlow(
                    f"top_p<1.0 未支持 (top_p={top_p}, subtalker_top_p={sub_top_p})"
                )
            if temperature <= 0 or sub_temp <= 0:
                raise FallbackToSlow("temperature<=0 未支持")

            L = int(embeds.shape[1])
            budget = self.max_cache_len - L - 2
            if budget <= 0:
                raise FallbackToSlow(f"prefill 长度 {L} 超出 max_cache_len")
            max_steps = min(max_new, self.max_frames, budget)

            key = (do_sample, top_k, sub_do, sub_top_k)
            graph = self._get_graph(key, do_sample, top_k, sub_do, sub_top_k)

            self._set_masks(eos_id, suppress)
            self.temperature_in.fill_(temperature)
            self.sub_temperature_in.fill_(sub_temp)
            self.rp_in.fill_(rp)

            # ---- prefill（eager，写 StaticCache 0..L-1） ----
            out = self.talker.forward(
                inputs_embeds=embeds,
                attention_mask=mask,
                past_key_values=self.talker_cache,
                trailing_text_hidden=trailing,
                tts_pad_embed=pad,
                generation_step=-1,
                use_cache=True,
                output_hidden_states=False,
                cache_position=torch.arange(L, device=self.dev),
            )
            logits0 = out.logits[:, -1, :].float()
            logits0 = logits0.masked_fill(self.suppress_mask, NEG_INF)
            if min_new >= 1:
                logits0 = logits0.masked_fill(self.eos_only_mask, NEG_INF)
            tok0 = self._sample_talker(logits0, do_sample, top_k)
            self.tok_in.copy_(tok0)
            self.past_hidden_in.copy_(out.past_hidden)
            self.hist.zero_()
            self.hist[0] = int(tok0.view(-1)[0])

            # ---- 帧循环（graph replay，控制流在图外） ----
            T = int(trailing.shape[1])
            codes_all = []
            step = 0
            while step < max_steps:
                if step < T:
                    self.text_embed_in.copy_(trailing[0, step].view(1, 1, -1))
                else:
                    self.text_embed_in.copy_(pad)
                self.t_cp_in.fill_(L + step)
                self.eos_allowed.fill_(step + 1 >= min_new)
                self.hist_len_in.fill_(step + 1)
                graph.replay()
                codes_all.append(self.codes_out.clone())
                step += 1
                if int(self.tok_in.view(-1)[0]) == eos_id:
                    break
            if not codes_all:
                raise FallbackToSlow("生成 0 帧")
            codes = torch.cat(codes_all, dim=0)
            self.last_codes = codes.cpu()
        finally:
            im.__exit__(None, None, None)

        # ---- codes -> wav（复刻 generate_voice_clone 的解码与 ICL 裁剪） ----
        ref_code = self._ref_code_of(voice_clone_prompt)
        if ref_code is not None:
            rc = ref_code.to(codes.device)
            codes_for_decode = torch.cat([rc, codes], dim=0)
        else:
            rc, codes_for_decode = None, codes
        wavs, sr = self.model.model.speech_tokenizer.decode([{"audio_codes": codes_for_decode}])
        wav = wavs[0]
        if rc is not None:
            ref_len, total = int(rc.shape[0]), int(codes_for_decode.shape[0])
            cut = int(ref_len / max(total, 1) * wav.shape[0])
            wav = wav[cut:]
        return [wav], sr
