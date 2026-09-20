import { useRef, useState } from "react";
import {
  ArrowRight, BookOpenText, CheckCircle2, ListMusic,
  Loader2, Mic, RefreshCw, Scissors, Square, Undo2, Upload, Wand2,
} from "lucide-react";
import type { useFt } from "@/pages/Ft/useFt";
import { SCRIPT_SENTENCES } from "@/pages/Ft/useFt";
import { mediaUrl } from "@/api/client";
import { useAppStore } from "@/store/useAppStore";
import { ErrorPanel } from "@/components/ErrorPanel";

const fmt = (s: number) => `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;

function Step({ n, title, active, done }: { n: number; title: string; active: boolean; done: boolean }) {
  return (
    <div className="flex items-center gap-2">
      <span className={`flex h-8 w-8 items-center justify-center rounded-full text-sm font-semibold shadow ${done ? "bg-primary text-primary-foreground" : active ? "border-2 border-primary bg-card text-primary" : "border border-border bg-card text-muted-foreground"}`}>{done ? <CheckCircle2 className="h-4 w-4" /> : n}</span>
      <span className={`text-sm ${active ? "font-medium text-foreground" : "text-muted-foreground"}`}>{title}</span>
    </div>
  );
}

export function FtPage(props: ReturnType<typeof useFt>) {
  const {
    voiceId, setVoiceId, displayName, status, trainStatus, error,
    recording, recSeconds, uploading, sentenceIdx, setSentenceIdx,
    startRecording, stopRecording, uploadFile,
    startTrain, audition, auditionText, setAuditionText, auditionBusy, doAudition,
    publishName, setPublishName, publishing, publishOk, setPublishOk, doPublish, removeFt, resetFlow,
    qc, qcBusy, qcMsg, keepGrades, setKeepGrades, runQc, pruneCorpus, restoreCorpus,
  } = props;
  const voices = useAppStore((s) => s.voices);
  const fileRef = useRef<HTMLInputElement>(null);

  const stage = status?.stage ?? "new";
  const ftVoices = Array.from(new Set([...(status ? [status.voice_id] : []), ...voices.filter((v) => v.kind === "finetuned").map((v) => v.id)])).filter(Boolean);
  // C2 续训：可选训练起点——默认 base 全量重训，或从任一已发布微调音色继续（含自身）
  const [initFrom, setInitFrom] = useState("");
  const publishedFtVoices = voices.filter((v) => v.kind === "finetuned").map((v) => v.id);

  const card = "rounded-xl border border-border bg-card/80 p-5 shadow-md backdrop-blur-md";
  const btn = "inline-flex items-center justify-center gap-2 rounded-lg px-4 py-2.5 text-sm font-medium shadow-sm transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary disabled:pointer-events-none disabled:opacity-50";
  const input = "w-full rounded-lg border border-border bg-background px-3 py-2.5 text-sm outline-none focus:border-primary focus:ring-2 focus:ring-primary/30";

  return (
    <div className="mx-auto max-w-4xl px-5 py-8 sm:px-8">
      <div className="flex items-end justify-between gap-4">
        <div>
          <p className="text-xs font-medium text-primary">微调工坊</p>
          <h2 className="mt-1 font-display text-xl font-semibold">音色微调工坊</h2>
          <p className="mt-2 max-w-xl text-sm leading-6 text-muted-foreground">
            朗读一篇短句（约 12~15 分钟），训练出「音色 + 说话习惯」都属于你的专属音色——比 3 秒克隆更像本人。全程本地训练，约 20~40 分钟。
          </p>
        </div>
        {voiceId && (
          <button type="button" onClick={resetFlow} className={`${btn} border border-border bg-card text-muted-foreground hover:text-foreground`}>
            新建微调
          </button>
        )}
      </div>

      {/* 步骤条 */}
      <div className="mt-6 flex flex-wrap items-center gap-x-6 gap-y-3">
        <Step n={1} title="录制 / 上传" active={!voiceId || stage === "new" || stage === "processing"} done={stage === "ready" || stage === "training" || stage === "trained" || stage === "published"} />
        <ArrowRight className="h-4 w-4 text-muted-foreground" />
        <Step n={2} title="训练" active={stage === "training"} done={stage === "trained" || stage === "published"} />
        <ArrowRight className="h-4 w-4 text-muted-foreground" />
        <Step n={3} title="试听对比" active={stage === "trained"} done={stage === "published"} />
        <ArrowRight className="h-4 w-4 text-muted-foreground" />
        <Step n={4} title="入音色库" active={stage === "trained"} done={stage === "published"} />
      </div>

      {error && (
        <ErrorPanel title="微调流程出错" detail={error} />
      )}

      {/* 历史微调音色 */}
      {ftVoices.length > 0 && (
        <div className={`${card} mt-6`}>
          <p className="flex items-center gap-2 text-sm font-medium"><ListMusic className="h-4 w-4 text-primary" />微调记录</p>
          <div className="mt-3 flex flex-wrap gap-2">
            {ftVoices.map((id) => (
              <span key={id} className="inline-flex items-center gap-2 rounded-full border border-border bg-background px-3 py-1.5 text-xs">
                {id}
                <button type="button" className="text-primary hover:underline" onClick={() => { setVoiceId(id); setPublishOk(false); }}>打开</button>
                <button type="button" className="text-destructive hover:underline" onClick={() => void removeFt(id)}>删除</button>
              </span>
            ))}
          </div>
        </div>
      )}

      {/* 步骤 1：录制 */}
      {(!voiceId || stage === "new" || stage === "processing") && (
        <div className={`${card} mt-6`}>
          {!voiceId ? (
            <>
              <label className="text-sm font-medium">给这个音色起个英文 ID（入库后用它选音色）</label>
              <input className={`${input} mt-2 max-w-xs`} placeholder="例如 my_voice" value={displayName} onChange={(e) => setVoiceId(e.target.value.replace(/[^a-zA-Z0-9_-]/g, ""))} />
              <p className="mt-2 text-xs text-muted-foreground">只允许字母 / 数字 / 下划线。确认后开始录音。</p>
            </>
          ) : stage === "processing" ? (
            <div className="flex items-center gap-3 py-6 text-sm text-muted-foreground">
              <Loader2 className="h-5 w-5 animate-spin text-primary" />
              {status?.message || "正在切句转写…"}{status?.clips ? `（已转写 ${status.clips} 条）` : ""}
            </div>
          ) : (
            <>
              <div className="flex flex-wrap items-center justify-between gap-3">
                <p className="flex items-center gap-2 text-sm font-medium"><BookOpenText className="h-4 w-4 text-primary" />对着下面的句子逐条朗读，读完一条点一条</p>
                <span className={`font-mono text-sm ${recording ? "text-destructive" : "text-muted-foreground"}`}>{recording ? "● " : ""}{fmt(recSeconds)}</span>
              </div>
              <div className="mt-3 max-h-72 space-y-1.5 overflow-y-auto rounded-lg border border-border bg-background p-4">
                {SCRIPT_SENTENCES.map((s, i) => (
                  <button type="button" key={i} onClick={() => setSentenceIdx(i)}
                    className={`block w-full rounded-md px-3 py-1.5 text-left text-sm transition ${i === sentenceIdx ? "bg-primary/15 font-medium text-primary" : i < sentenceIdx ? "text-muted-foreground" : "text-foreground hover:bg-muted"}`}>
                    <span className="mr-2 font-mono text-xs text-muted-foreground">{i + 1}.</span>{s}
                  </button>
                ))}
              </div>
              <div className="mt-4 flex flex-wrap items-center gap-3">
                {!recording ? (
                  <button type="button" onClick={() => void startRecording()} className={`${btn} bg-primary text-primary-foreground hover:bg-primary/90`}><Mic className="h-4 w-4" />开始录音</button>
                ) : (
                  <button type="button" onClick={() => void stopRecording()} className={`${btn} bg-destructive text-destructive-foreground hover:bg-destructive/90`}><Square className="h-4 w-4" />停止并上传（{fmt(recSeconds)}）</button>
                )}
                <button type="button" onClick={() => fileRef.current?.click()} className={`${btn} border border-border bg-card text-muted-foreground hover:text-foreground`}><Upload className="h-4 w-4" />改为上传音频文件</button>
                <input ref={fileRef} type="file" accept="audio/*,.webm,.m4a,.wav,.mp3" className="hidden" onChange={(e) => { const f = e.target.files?.[0]; if (f) void uploadFile(f); }} />
                {uploading && <span className="flex items-center gap-2 text-sm text-muted-foreground"><Loader2 className="h-4 w-4 animate-spin" />上传中…</span>}
              </div>
              <p className="mt-3 text-xs leading-5 text-muted-foreground">
                建议：安静房间、离麦 20~30cm、正常语速读完 60 句以上。录完点「停止并上传」，处理完自动进入下一步。
              </p>
            </>
          )}
        </div>
      )}

      {/* 步骤 1.5：处理结果 */}
      {stage === "ready" && (
        <div className={`${card} mt-6`}>
          <p className="text-sm font-medium">处理完成</p>
          <div className="mt-3 grid grid-cols-2 gap-3 sm:grid-cols-4">
            {[["有效样本", `${status?.clips ?? "-"} 条`], ["录音时长", `${status?.duration_s ?? "-"} s`], ["语音净时长", `${status?.speech_s ?? "-"} s`], ["锚点参考", status?.anchor ?? "-"]].map(([k, v]) => (
              <div key={k} className="rounded-lg border border-border bg-background p-3">
                <p className="text-xs text-muted-foreground">{k}</p>
                <p className="mt-1 truncate text-sm font-medium">{v}</p>
              </div>
            ))}
          </div>
          {status?.transcripts && status.transcripts.length > 0 && (
            <div className="mt-4 rounded-lg border border-border bg-background p-3">
              <p className="text-xs text-muted-foreground">转写抽查（前 8 条）</p>
              <div className="mt-2 space-y-1 text-sm">
                {status.transcripts.map((t) => (
                  <p key={t.name} className="truncate"><span className="mr-2 font-mono text-xs text-muted-foreground">{t.name}</span>{t.text}</p>
                ))}
              </div>
            </div>
          )}
          {status?.speech_s != null && status.speech_s < 180 && (
            <p className="mt-3 rounded-lg border border-yellow-500/40 bg-yellow-500/10 px-3 py-2 text-xs leading-5 text-yellow-600">
              语音净时长只有 {status.speech_s}s（建议 ≥5 分钟）。可以先用它验证流程，但正式音色建议重录更长。
            </p>
          )}

          {/* 语料体检 */}
          <div className="mt-4 rounded-lg border border-border bg-background p-3">
            <div className="flex items-center justify-between">
              <p className="text-sm font-medium">语料体检{qc && <span className="ml-2 text-xs font-normal text-muted-foreground">均分 {qc.avg_score.toFixed(1)}</span>}</p>
              <button type="button" disabled={qcBusy} onClick={() => void runQc(true)}
                className="inline-flex items-center gap-1 rounded-md px-2 py-1 text-xs text-muted-foreground hover:text-foreground disabled:opacity-50">
                {qcBusy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5" />}重新体检
              </button>
            </div>
            {qc ? (
              <>
                <div className="mt-2 flex flex-wrap items-center gap-2 text-xs">
                  {(["A", "B", "C", "D"] as const).map((g) => (
                    <span key={g} className={`rounded-full px-2 py-0.5 font-medium ${
                      g === "A" ? "bg-emerald-500/15 text-emerald-600" : g === "B" ? "bg-sky-500/15 text-sky-600"
                      : g === "C" ? "bg-yellow-500/15 text-yellow-600" : "bg-destructive/15 text-destructive"}`}>
                      {g} 级 {qc.grades[g]}
                    </span>
                  ))}
                  <span className="text-muted-foreground">共 {qc.count} 条 · 净时长 {Math.round(qc.total_s)}s{qc.rejected_count > 0 && ` · 历史已剔 ${qc.rejected_count} 条`}</span>
                </div>
                {qc.grades.D > 0 && (
                  <p className="mt-2 text-xs leading-5 text-destructive">
                    检出 {qc.grades.D} 条 D 级废片{qc.top_reasons.slice(0, 2).map((r) => `（${r.reason}×${r.count}）`).join("")}——这些切片会污染音色，建议剔除后再训练。
                  </p>
                )}
                {qc.grades.D === 0 && qc.grades.C > 0 && (
                  <p className="mt-2 text-xs leading-5 text-yellow-600">有 {qc.grades.C} 条 C 级切片质量偏差，默认保留；追求效果可在剔除时一并移除。</p>
                )}
                {qc.grades.D === 0 && qc.grades.C === 0 && (
                  <p className="mt-2 text-xs text-emerald-600">语料干净，可以直接开训。</p>
                )}
                {(qc.grades.C > 0 || qc.grades.D > 0) && (
                  <div className="mt-3 flex flex-wrap items-center gap-2">
                    <div className="flex overflow-hidden rounded-lg border border-border text-xs">
                      {["A,B", "A,B,C"].map((k) => (
                        <button key={k} type="button" onClick={() => setKeepGrades(k)}
                          className={`px-2.5 py-1.5 ${keepGrades === k ? "bg-primary text-primary-foreground" : "bg-card text-muted-foreground hover:text-foreground"}`}>
                          {k === "A,B" ? "剔到只剩 A/B" : "C 级也剔"}
                        </button>
                      ))}
                    </div>
                    <button type="button" disabled={qcBusy} onClick={() => void pruneCorpus()}
                      className="inline-flex items-center gap-1.5 rounded-lg border border-destructive/40 px-3 py-1.5 text-xs font-medium text-destructive hover:bg-destructive/10 disabled:opacity-50">
                      <Scissors className="h-3.5 w-3.5" />一键剔除并重选锚点
                    </button>
                    <button type="button" disabled={qcBusy || qc.rejected_count === 0} onClick={() => void restoreCorpus()}
                      className="inline-flex items-center gap-1.5 rounded-lg border border-border px-3 py-1.5 text-xs text-muted-foreground hover:text-foreground disabled:opacity-50">
                      <Undo2 className="h-3.5 w-3.5" />恢复全部
                    </button>
                  </div>
                )}
                {qc.advice.length > 0 && (
                  <p className="mt-2 text-xs leading-5 text-muted-foreground">{qc.advice.join("；")}</p>
                )}
              </>
            ) : (
              <p className="mt-2 text-xs text-muted-foreground">{qcBusy ? "体检中…" : "暂无体检结果"}</p>
            )}
            {qcMsg && <p className="mt-2 rounded-md border border-yellow-500/40 bg-yellow-500/10 px-2.5 py-1.5 text-xs leading-5 text-yellow-600">{qcMsg}</p>}
          </div>

          <div className="mt-4 flex flex-wrap items-center gap-2">
            {[8, 12, 20].map((n) => (
              <button key={n} type="button" onClick={() => void startTrain(n, initFrom)} className={`${btn} ${n === 12 ? "bg-primary text-primary-foreground hover:bg-primary/90" : "border border-border bg-card text-muted-foreground hover:text-foreground"}`}>
                <Wand2 className="h-4 w-4" />训练 {n} 轮
              </button>
            ))}
            <select value={initFrom} onChange={(e) => setInitFrom(e.target.value)}
              className="ml-auto rounded-lg border border-border bg-card px-2.5 py-2 text-xs text-muted-foreground outline-none focus:border-primary" title="训练起点：从零开始或基于已有微调模型继续">
              <option value="">训练起点：base 全量重训</option>
              {publishedFtVoices.map((v) => (
                <option key={v} value={v}>续训自「{v}」（快，增量加料）</option>
              ))}
            </select>
          </div>
          {initFrom && (
            <p className="mt-2 text-xs leading-5 text-muted-foreground">
              将从「{initFrom}」已训练的模型继续（素材仍是当前录音，适合补录后增量加料）；想完全重来请切回 base 全量重训。
            </p>
          )}
          <p className="mt-2 text-xs text-muted-foreground">8GB 显存约 0.5~1 分钟/轮，12 轮约 10 分钟</p>
        </div>
      )}

      {/* 步骤 2：训练进度 */}
      {(stage === "training" || stage === "trained" || stage === "published") && trainStatus && (
        <div className={`${card} mt-6`}>
          <div className="flex items-center justify-between">
            <p className="text-sm font-medium">{stage === "training" ? "训练中" : "训练完成"}</p>
            {trainStatus.running && <Loader2 className="h-4 w-4 animate-spin text-primary" />}
          </div>
          {stage === "training" && (
            <div className="mt-3">
              <div className="h-2 overflow-hidden rounded-full bg-muted">
                <div className="h-full rounded-full bg-primary transition-all" style={{ width: `${Math.min(100, ((trainStatus.epoch ?? 0) + 1) / (trainStatus.epochs || 12) * 100)}%` }} />
              </div>
              <div className="mt-2 flex flex-wrap gap-x-6 gap-y-1 text-xs text-muted-foreground">
                <span>epoch {trainStatus.epoch ?? 0}/{trainStatus.epochs ?? "-"}</span>
                {trainStatus.loss != null && <span>loss {trainStatus.loss.toFixed(2)}</span>}
                {trainStatus.vram_peak != null && <span>峰值显存 {trainStatus.vram_peak.toFixed(2)} GiB</span>}
              </div>
            </div>
          )}
          {trainStatus.error && <ErrorPanel title="RVC 训练失败" detail={trainStatus.error} />}
          {trainStatus.log_tail.length > 0 && (
            <pre className="mt-3 max-h-40 overflow-y-auto rounded-lg border border-border bg-background p-3 font-mono text-[11px] leading-5 text-muted-foreground">{trainStatus.log_tail.join("\n")}</pre>
          )}
        </div>
      )}

      {/* 步骤 3+4：试听 & 入库 */}
      {(stage === "trained" || stage === "published") && (
        <div className={`${card} mt-6`}>
          <p className="text-sm font-medium">试听对比：微调版 vs 声纹克隆版</p>
          <div className="mt-3 flex flex-col gap-2 sm:flex-row">
            <input className={input} value={auditionText} onChange={(e) => setAuditionText(e.target.value)} placeholder="输入没在录音里出现的句子" />
            <button type="button" onClick={() => void doAudition()} disabled={auditionBusy} className={`${btn} shrink-0 bg-primary text-primary-foreground hover:bg-primary/90`}>
              {auditionBusy ? <Loader2 className="h-4 w-4 animate-spin" /> : <Mic className="h-4 w-4" />}生成对比
            </button>
          </div>
          {audition && (
            <div className="mt-4 grid gap-3 sm:grid-cols-2">
              <div className="rounded-lg border border-primary/40 bg-primary/5 p-3">
                <p className="text-xs font-medium text-primary">微调版（音色+说话习惯）</p>
                <audio controls className="mt-2 w-full" src={mediaUrl(audition.tuned_url)} />
              </div>
              {audition.xvec_url && (
                <div className="rounded-lg border border-border bg-background p-3">
                  <p className="text-xs text-muted-foreground">声纹克隆版（仅音色）</p>
                  <audio controls className="mt-2 w-full" src={mediaUrl(audition.xvec_url)} />
                </div>
              )}
            </div>
          )}
          <div className="mt-5 border-t border-border pt-4">
            {stage === "published" || publishOk ? (
              <p className="flex items-center gap-2 text-sm text-primary"><CheckCircle2 className="h-4 w-4" />已入音色库，可在「文字转语音」里直接选用「{publishName || displayName || voiceId}」。</p>
            ) : (
              <div className="flex flex-col gap-2 sm:flex-row">
                <input className={input} value={publishName} onChange={(e) => setPublishName(e.target.value)} placeholder={`显示名（默认 ${displayName || voiceId}）`} />
                <button type="button" onClick={() => void doPublish()} disabled={publishing} className={`${btn} shrink-0 bg-primary text-primary-foreground hover:bg-primary/90`}>
                  {publishing ? <Loader2 className="h-4 w-4 animate-spin" /> : <CheckCircle2 className="h-4 w-4" />}确认入音色库
                </button>
              </div>
            )}
          </div>
        </div>
      )}

      {stage === "error" && status?.error && (
        <div className={`${card} mt-6`}>
          <ErrorPanel title="微调处理失败" detail={status.error} />
          <button type="button" onClick={resetFlow} className={`${btn} mt-4 border border-border bg-card`}>重新开始</button>
        </div>
      )}
    </div>
  );
}
