import { Ionicons } from "@expo/vector-icons";
import { router } from "expo-router";
import React, { useCallback, useState } from "react";
import { Pressable, RefreshControl, ScrollView, StyleSheet, Text, View } from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";

import {
  CascadeStatus,
  HealthInfo,
  PipelineStatus,
  RvcLiveStatus,
  RvcVoice,
  TrainStatus,
  VoiceInfo,
  cascadeStart,
  cascadeStatus,
  cascadeStop,
  getHealth,
  getPipelineStatus,
  getTrainStatus,
  listRvcVoices,
  listVoices,
  rvcLiveStart,
  rvcLiveStatus,
  rvcLiveStop,
} from "@/src/api";
import { useAppStore } from "@/src/store";
import { pluginVisible, useCapabilities } from "@/src/capabilities";
import { Badge, Button, C, Card, Row, StatPill, usePolling } from "@/src/ui";

const STAGE_ZH: Record<string, string> = {
  idle: "空闲",
  init: "初始化",
  warming: "模型预热",
  capturing: "采集中",
  asr: "识别中",
  tts: "合成中",
  playing: "播出中",
  error: "错误",
};

export default function HomeScreen() {
  const insets = useSafeAreaInsets();
  const { host, lastVoiceId, setLastVoiceId } = useAppStore();
  const caps = useCapabilities();
  // 级联/实时/train 都属于 sound.rvc-live；素材流水线属于 sound.workshop。
  // 清单没拿到时 pluginVisible 恒 true，全部照常显示（fail-open）。
  const rvcLiveVisible = pluginVisible(caps, "sound.rvc-live");
  const workshopVisible = pluginVisible(caps, "sound.workshop");
  const [health, setHealth] = useState<HealthInfo | null>(null);
  const [online, setOnline] = useState(false);
  const [cs, setCs] = useState<CascadeStatus | null>(null);
  const [live, setLive] = useState<RvcLiveStatus | null>(null);
  const [voices, setVoices] = useState<VoiceInfo[]>([]);
  const [voiceId, setVoiceId] = useState<string | null>(lastVoiceId);
  const [rvcVoices, setRvcVoices] = useState<RvcVoice[]>([]);
  const [liveExp, setLiveExp] = useState<string | null>(null);
  const [train, setTrain] = useState<TrainStatus | null>(null);
  const [pipeline, setPipeline] = useState<PipelineStatus | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState("");
  const [retryPending, setRetryPending] = useState(false);

  // 后端连通性（5s）
  usePolling(
    async () => {
      try {
        const h = await getHealth();
        setHealth(h);
        setOnline(true);
      } catch {
        setOnline(false);
      }
    },
    5000,
    true
  );

  // 音色列表（30s 拉一次）
  usePolling(
    async () => {
      try {
        const vs = await listVoices();
        setVoices(vs);
        if (!voiceId && vs.length > 0) {
          setVoiceId(vs[0].id);
          setLastVoiceId(vs[0].id);
        }
        // RVC 可用音色（实时变声需 model_ready）
        const info = await listRvcVoices();
        const ready = info.voices.filter((v) => v.model_ready);
        setRvcVoices(ready);
        if (!liveExp && ready.length > 0) setLiveExp(ready[0].id);
      } catch {
        /* ignore */
      }
    },
    30000,
    true
  );

  // 级联 + 实时状态（1s）
  usePolling(
    async () => {
      if (!rvcLiveVisible) return;
      try {
        const s = await cascadeStatus();
        setCs(s);
        // worker 预热完成 → 自动重试 start（对齐 PC 端懒启动语义）
        if (retryPending && s.worker_ready && !s.running) {
          setRetryPending(false);
          await doStart();
        } else if (s.running) {
          setRetryPending(false);
        }
        const l = await rvcLiveStatus();
        setLive(l);
      } catch {
        /* 断连时保持上次状态 */
      }
    },
    1000,
    true
  );

  // 训练 + 流水线进度（3s，只读监控，断连静默）
  usePolling(
    async () => {
      if (rvcLiveVisible) {
        try {
          setTrain(await getTrainStatus(liveExp ?? undefined));
        } catch { /* ignore */ }
      }
      if (workshopVisible) {
        try {
          setPipeline(await getPipelineStatus());
        } catch { /* ignore */ }
      }
    },
    3000,
    true
  );

  const running = cs?.running ?? false;
  const liveRunning = live?.live_running ?? false;

  const doStart = useCallback(async () => {
    setBusy("cascade");
    setErr("");
    try {
      const r = await cascadeStart(voiceId ? { voiceId } : undefined);
      if (r.warming) setRetryPending(true);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }, [voiceId]);

  const doStop = useCallback(async () => {
    setBusy("cascade");
    setErr("");
    try {
      await cascadeStop();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }, []);

  const doLiveToggle = useCallback(async () => {
    setBusy("live");
    setErr("");
    try {
      if (liveRunning) {
        await rvcLiveStop();
      } else {
        await rvcLiveStart(liveExp ?? undefined);
      }
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }, [liveRunning, liveExp]);

  const stageZh = cs ? (STAGE_ZH[cs.stage] ?? cs.stage) : "—";
  const warming = retryPending || (cs?.warming ?? false);

  return (
    <View style={styles.root}>
      {/* 顶栏 */}
      <View style={[styles.topbar, { paddingTop: insets.top + 12 }]}>
        <View>
          <Text style={styles.title}>变声工坊</Text>
          <Text style={styles.subtitle} numberOfLines={1}>
            {online ? `${host.replace(/^https?:\/\//, "")}${health?.cuda ? " · CUDA" : ""}` : "未连接"}
          </Text>
        </View>
        <Pressable onPress={() => router.push("/settings")} style={styles.gear}>
          <Ionicons name="settings-outline" size={22} color={C.sub} />
        </Pressable>
      </View>

      <ScrollView
        contentContainerStyle={styles.body}
        refreshControl={
          <RefreshControl refreshing={false} onRefresh={() => getHealth().then((h) => { setHealth(h); setOnline(true); }).catch(() => setOnline(false))} />
        }>
        {/* 连接状态 */}
        <Card>
          <View style={styles.rowBetween}>
            <Text style={styles.cardTitle}>后端连接</Text>
            <Badge text={online ? "在线" : "离线"} tone={online ? "ok" : "err"} />
          </View>
          {!online && (
            <Text style={styles.hint}>
              无法连接 PC 后端。请确认手机与电脑在同一 WiFi，且电脑端变声工坊已启动。可在右上角设置中修改地址。
            </Text>
          )}
        </Card>

        {/* 级联变声遥控（sound.rvc-live） */}
        {rvcLiveVisible && (
          <>
            <Text style={styles.section}>级联变声（文字中转 · 消除口音）</Text>
            <Card>
          <View style={styles.rowBetween}>
            <View style={{ flexDirection: "row", alignItems: "center", gap: 8 }}>
              <Text style={styles.cardTitle}>{running ? "运行中" : "已停止"}</Text>
              {running && <Badge text={stageZh} tone="tint" />}
            </View>
            {cs && (
              <Text style={styles.mini}>
                块 {cs.chunks} · 弃 {cs.dropped}
              </Text>
            )}
          </View>

          {running && cs && (
            <>
              {cs.last_text ? (
                <View style={styles.textBox}>
                  <Text style={styles.text}>{cs.last_text}</Text>
                </View>
              ) : null}
              <View style={styles.stats}>
                <StatPill label="平均延迟" value={cs.avg_latency_s ? `${cs.avg_latency_s.toFixed(2)}s` : "—"} />
                <StatPill label="待播" value={`${cs.queued_s.toFixed(1)}s`} />
                <StatPill label="识别均耗时" value={cs.avg_asr_s ? `${cs.avg_asr_s.toFixed(2)}s` : "—"} />
                <StatPill label="合成均耗时" value={cs.avg_tts_s ? `${cs.avg_tts_s.toFixed(2)}s` : "—"} />
              </View>
              <Row label="输入设备" value={cs.input_device} />
              <Row label="输出设备" value={cs.output_device} />
            </>
          )}

          {/* 音色选择 */}
          <Text style={[styles.label, { marginTop: 12 }]}>合成音色</Text>
          <ScrollView horizontal showsHorizontalScrollIndicator={false} contentContainerStyle={{ gap: 8, paddingTop: 4 }}>
            {voices.map((v) => {
              const active = v.id === voiceId;
              return (
                <Pressable
                  key={v.id}
                  onPress={() => {
                    setVoiceId(v.id);
                    setLastVoiceId(v.id);
                  }}
                  style={[styles.chip, active && styles.chipActive]}>
                  <Text style={[styles.chipText, active && styles.chipTextActive]}>{v.display_name || v.id}</Text>
                </Pressable>
              );
            })}
            {voices.length === 0 && <Text style={styles.hint}>暂无音色（PC 端未入库）</Text>}
          </ScrollView>

          <Button
            title={
              busy === "cascade"
                ? "处理中…"
                : warming
                  ? "模型预热中…"
                  : running
                    ? "停止级联变声"
                    : "启动级联变声"
            }
            tone={running ? "danger" : "primary"}
            onPress={running ? doStop : doStart}
            disabled={busy === "cascade" || warming}
            style={{ marginTop: 14 }}
          />
          {err ? <Text style={styles.err}>{err}</Text> : null}
          {(cs?.last_error || cs?.child_error) ? (
            <Text style={styles.err} numberOfLines={3}>
              {cs.last_error || cs.child_error}
            </Text>
          ) : null}
          <Text style={styles.hint}>PC 端全局热键 Ctrl+Alt+V 可随时启停</Text>
            </Card>
          </>
        )}

        {/* RVC 实时变声遥控（sound.rvc-live） */}
        {rvcLiveVisible && (
          <>
            <Text style={styles.section}>RVC 实时变声（遥控 PC）</Text>
            <Card>
          <View style={styles.rowBetween}>
            <Text style={styles.cardTitle}>{liveRunning ? "运行中" : "已停止"}</Text>
            {live ? <Badge text={live.exp} tone="gray" /> : null}
          </View>
          {live && !live.model_ok && live.model_detail ? (
            <Text style={styles.err} numberOfLines={2}>
              {live.model_detail}
            </Text>
          ) : null}

          {/* 实时转写字幕：与 PC 端字幕同源（--asr-only 子进程），说话即显示 */}
          {liveRunning && live?.asr_running && live.asr_last_text ? (
            <View style={styles.textBox}>
              <Text style={styles.text}>{live.asr_last_text}</Text>
            </View>
          ) : null}

          {/* RVC 音色选择（需 model_ready） */}
          {!liveRunning && (
            <>
              <Text style={[styles.label, { marginTop: 12 }]}>RVC 音色</Text>
              <ScrollView horizontal showsHorizontalScrollIndicator={false} contentContainerStyle={{ gap: 8, paddingTop: 4 }}>
                {rvcVoices.map((v) => {
                  const active = v.id === liveExp;
                  return (
                    <Pressable
                      key={v.id}
                      onPress={() => setLiveExp(v.id)}
                      style={[styles.chip, active && styles.chipActive]}>
                      <Text style={[styles.chipText, active && styles.chipTextActive]}>{v.display_name || v.id}</Text>
                    </Pressable>
                  );
                })}
                {rvcVoices.length === 0 && (
                  <Text style={styles.hint}>暂无已就绪的 RVC 音色（需在 PC 端完成训练）</Text>
                )}
              </ScrollView>
            </>
          )}

          <Button
            title={busy === "live" ? "处理中…" : liveRunning ? "停止实时变声" : "启动实时变声"}
            tone={liveRunning ? "danger" : "primary"}
            onPress={doLiveToggle}
            disabled={busy === "live" || !online || (!liveRunning && rvcVoices.length === 0)}
            style={{ marginTop: 12 }}
          />
          <Text style={styles.hint}>麦克风与声卡切换都在 PC 端完成，与级联变声互斥</Text>
            </Card>
          </>
        )}

        {/* PC 端任务状态镜像：训练（sound.rvc-live）/ 素材流水线（sound.workshop） */}
        {(rvcLiveVisible || workshopVisible) && (
          <>
            <Text style={styles.section}>PC 任务状态</Text>
            {rvcLiveVisible && (
              <Card>
          <View style={styles.rowBetween}>
            <Text style={styles.cardTitle}>RVC 训练</Text>
            {train ? (
              <Badge
                text={train.running ? `${train.percent.toFixed(0)}%` : train.done ? "已完成" : train.error ? "出错" : "空闲"}
                tone={train.running ? "tint" : train.done ? "ok" : train.error ? "err" : "gray"}
              />
            ) : null}
          </View>
          {train?.running ? (
            <>
              <View style={styles.barTrack}>
                <View style={[styles.barFill, { width: `${Math.min(100, Math.max(0, train.percent))}%` }]} />
              </View>
              <Text style={styles.hint} numberOfLines={2}>
                {train.exp} · {train.stage || train.message || "进行中"} · 数据集 {train.dataset_count} 条
              </Text>
            </>
          ) : train?.error ? (
            <Text style={styles.err} numberOfLines={2}>{train.error}</Text>
          ) : (
            <Text style={styles.hint}>PC 端当前没有训练任务{train?.exp ? `（最近：${train.exp}）` : ""}</Text>
          )}
        </Card>
            )}
            {workshopVisible && (
              <Card>
          <View style={styles.rowBetween}>
            <Text style={styles.cardTitle}>素材流水线</Text>
            {pipeline ? (
              <Badge
                text={
                  pipeline.status === "running" ? `${pipeline.percent.toFixed(0)}%`
                  : pipeline.status === "done" ? "已完成"
                  : pipeline.status === "error" ? "出错"
                  : pipeline.status === "cancelled" ? "已取消"
                  : "空闲"
                }
                tone={pipeline.status === "running" ? "tint" : pipeline.status === "done" ? "ok" : pipeline.status === "error" ? "err" : "gray"}
              />
            ) : null}
          </View>
          {pipeline?.status === "running" ? (
            <>
              <View style={styles.barTrack}>
                <View style={[styles.barFill, { width: `${Math.min(100, Math.max(0, pipeline.percent))}%` }]} />
              </View>
              <Text style={styles.hint} numberOfLines={2}>
                {pipeline.step || "处理中"}{pipeline.message ? ` · ${pipeline.message}` : ""} · 已产出 {pipeline.clips} 切片
              </Text>
            </>
          ) : pipeline?.error ? (
            <Text style={styles.err} numberOfLines={2}>{pipeline.error}</Text>
          ) : (
            <Text style={styles.hint}>
              素材挖掘/切片流水线{pipeline?.clips ? `（库存 ${pipeline.clips} 切片）` : ""}
            </Text>
          )}
        </Card>
            )}
          </>
        )}
      </ScrollView>
    </View>
  );
}

const styles = StyleSheet.create({
  root: { flex: 1, backgroundColor: C.bg },
  topbar: {
    paddingHorizontal: 20,
    paddingTop: 12,
    paddingBottom: 12,
    backgroundColor: "#fff",
    borderBottomWidth: 1,
    borderBottomColor: C.border,
    flexDirection: "row",
    justifyContent: "space-between",
    alignItems: "center",
  },
  title: { fontSize: 24, fontWeight: "800", color: C.text },
  subtitle: { fontSize: 12, color: C.sub, marginTop: 2 },
  gear: { padding: 8 },
  body: { padding: 16, paddingBottom: 40 },
  section: { fontSize: 13, fontWeight: "600", color: C.sub, marginBottom: 8, marginTop: 20, letterSpacing: 0.5 },
  cardTitle: { fontSize: 16, fontWeight: "700", color: C.text },
  rowBetween: { flexDirection: "row", justifyContent: "space-between", alignItems: "center" },
  mini: { fontSize: 11, color: C.sub, fontVariant: ["tabular-nums"] },
  textBox: {
    backgroundColor: "#F8FAFB",
    borderRadius: 10,
    padding: 12,
    marginTop: 12,
    borderLeftWidth: 3,
    borderLeftColor: C.tint,
  },
  text: { fontSize: 14, color: C.text, lineHeight: 21 },
  stats: { flexDirection: "row", gap: 8, marginTop: 12 },
  label: { fontSize: 12, color: C.sub, fontWeight: "600" },
  chip: {
    paddingHorizontal: 14,
    paddingVertical: 8,
    borderRadius: 999,
    backgroundColor: "#F1F3F5",
    borderWidth: 1,
    borderColor: "transparent",
  },
  chipActive: { backgroundColor: C.tintSoft, borderColor: C.tint },
  chipText: { fontSize: 13, color: C.text },
  chipTextActive: { color: C.tint, fontWeight: "600" },
  hint: { fontSize: 12, color: C.sub, marginTop: 10, lineHeight: 17 },
  err: { fontSize: 12, color: C.err, marginTop: 8, lineHeight: 17 },
  barTrack: {
    height: 6,
    borderRadius: 3,
    backgroundColor: "#EEF1F4",
    overflow: "hidden",
    marginTop: 12,
  },
  barFill: { height: 6, borderRadius: 3, backgroundColor: C.tint },
});
