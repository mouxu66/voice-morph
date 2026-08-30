import { Ionicons } from "@expo/vector-icons";
import React, { useEffect, useRef, useState } from "react";
import { Pressable, ScrollView, StyleSheet, Switch, Text, View } from "react-native";
import { Audio } from "expo-av";
import Slider from "@react-native-community/slider";
import { useSafeAreaInsets } from "react-native-safe-area-context";

import { OfflineVcStatus, RvcVoice, getOfflineVcStatus, listRvcVoices, mediaUrl, runOfflineVc } from "@/src/api";
import { Badge, Button, C, Card, PlayButton, Row, ShareButton, usePolling } from "@/src/ui";

// 转换任务兜底超时：后端 RVC 推理子进程 timeout 1800s，留出余量覆盖后端失联场景
const TASK_TIMEOUT_MS = 35 * 60 * 1000;
// 提交后宽限期：后端提交接口同步置 running，若轮询到 idle 说明后端重启丢了状态
const IDLE_GRACE_MS = 8000;

export default function VcScreen() {
  const insets = useSafeAreaInsets();
  const [voices, setVoices] = useState<RvcVoice[]>([]);
  const [voiceId, setVoiceId] = useState<string | null>(null);
  const [recording, setRecording] = useState(false);
  const [recSecs, setRecSecs] = useState(0);
  const [recUri, setRecUri] = useState<string | null>(null);
  const [pitch, setPitch] = useState(0);
  const [indexRate, setIndexRate] = useState(0.5);
  const [denoise, setDenoise] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [taskRunning, setTaskRunning] = useState(false);
  const [err, setErr] = useState("");
  const [result, setResult] = useState<OfflineVcStatus | null>(null);

  const recRef = useRef<Audio.Recording | null>(null);
  const submittedAtRef = useRef(0);

  useEffect(() => {
    listRvcVoices()
      .then((info) => {
        const ready = info.voices.filter((v) => v.model_ready);
        setVoices(ready);
        if (ready.length > 0) setVoiceId(ready[0].id);
      })
      .catch(() => setErr("无法获取音色列表，请检查后端连接"));
    return () => {
      recRef.current?.stopAndUnloadAsync().catch(() => {});
    };
  }, []);

  // 转换任务进行中 → 轮询进度。
  // 后端状态在内存中：任务期间 PC 端重启会永远停在 idle，必须靠前端兜底复位，
  // 否则按钮卡在「转换中…」只能杀 App。
  usePolling(
    async () => {
      const s = await getOfflineVcStatus();
      if (s.status === "running" || s.running) {
        setResult(s);
        return;
      }
      if (s.status === "done" && s.url) {
        setResult(s);
        setTaskRunning(false);
        return;
      }
      if (s.status === "error") {
        setErr(s.error || "转换失败");
        setTaskRunning(false);
        return;
      }
      // idle：提交成功后后端必为 running，拿到 idle 说明状态丢失（后端重启）
      if (Date.now() - submittedAtRef.current > IDLE_GRACE_MS) {
        setTaskRunning(false);
        setErr("任务状态丢失（PC 端后端可能已重启），请重新提交");
      }
    },
    1500,
    taskRunning
  );

  // 挂钟兜底：后端进程被杀（轮询静默失败）或任务卡死时，到点强制复位按钮
  useEffect(() => {
    if (!taskRunning) return;
    const t = setTimeout(() => {
      setTaskRunning(false);
      setErr("转换超时，请重试");
    }, TASK_TIMEOUT_MS);
    return () => clearTimeout(t);
  }, [taskRunning]);

  const startRec = async () => {
    try {
      setErr("");
      setResult(null);
      const perm = await Audio.requestPermissionsAsync();
      if (!perm.granted) {
        setErr("需要麦克风权限才能录音");
        return;
      }
      await Audio.setAudioModeAsync({
        allowsRecordingIOS: true,
        playsInSilentModeIOS: true,
      });
      const rec = new Audio.Recording();
      rec.setOnRecordingStatusUpdate((s) => {
        if (s.isRecording && s.durationMillis) setRecSecs(s.durationMillis / 1000);
      });
      await rec.prepareToRecordAsync(Audio.RecordingOptionsPresets.HIGH_QUALITY);
      await rec.startAsync();
      recRef.current = rec;
      setRecording(true);
      setRecSecs(0);
      setRecUri(null);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "录音启动失败");
    }
  };

  const stopRec = async () => {
    try {
      const rec = recRef.current;
      if (!rec) return;
      await rec.stopAndUnloadAsync();
      const uri = rec.getURI();
      setRecUri(uri ?? null);
      recRef.current = null;
      setRecording(false);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "录音结束失败");
      setRecording(false);
    }
  };

  const submit = async () => {
    if (!recUri || !voiceId) return;
    setErr("");
    setResult(null);
    setSubmitting(true);
    try {
      await runOfflineVc(recUri, voiceId, pitch, indexRate, denoise);
      submittedAtRef.current = Date.now();
      setTaskRunning(true);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "提交失败");
    } finally {
      setSubmitting(false);
    }
  };

  const converting = submitting || taskRunning;

  return (
    <ScrollView style={styles.root} contentContainerStyle={[styles.body, { paddingTop: insets.top + 12 }]}>
      <Text style={styles.title}>离线变声</Text>
      <Text style={styles.subtitle}>手机录音 → 上传 PC 用 RVC 转换 → 回传播放</Text>

      {/* 录音区 */}
      <Card style={{ alignItems: "center", paddingVertical: 28 }}>
        <Pressable
          onPress={recording ? stopRec : startRec}
          style={({ pressed }) => [
            styles.recBtn,
            recording && styles.recBtnActive,
            pressed && { transform: [{ scale: 0.97 }] },
          ]}>
          <Ionicons name={recording ? "stop" : "mic"} size={36} color={recording ? "#fff" : C.tint} />
        </Pressable>
        <Text style={styles.recText}>{recording ? `${recSecs.toFixed(1)}s · 点击停止` : recUri ? "已录音，点击重录" : "点击开始录音"}</Text>
        {recUri && !recording && <Badge text={`已录 ${recSecs.toFixed(1)} 秒`} tone="ok" />}
      </Card>

      {/* 音色 */}
      <Text style={styles.section}>变声音色（需 PC 端已完成训练）</Text>
      <ScrollView horizontal showsHorizontalScrollIndicator={false} contentContainerStyle={{ gap: 8 }}>
        {voices.map((v) => (
          <Pressable
            key={v.id}
            onPress={() => setVoiceId(v.id)}
            style={[styles.chip, v.id === voiceId && styles.chipActive]}>
            <Text style={[styles.chipText, v.id === voiceId && styles.chipTextActive]}>{v.display_name || v.id}</Text>
          </Pressable>
        ))}
        {voices.length === 0 && <Text style={styles.hint}>暂无可用音色（需在 PC 端完成至少一个音色训练）</Text>}
      </ScrollView>

      {/* 参数 */}
      <Text style={styles.section}>转换参数</Text>
      <Card>
        <View style={styles.paramHead}>
          <Text style={styles.paramLabel}>音调（半音）</Text>
          <Text style={styles.paramValue}>{pitch > 0 ? `+${pitch}` : pitch}</Text>
        </View>
        <Slider
          minimumValue={-12}
          maximumValue={12}
          step={1}
          value={pitch}
          onValueChange={setPitch}
          minimumTrackTintColor={C.tint}
          maximumTrackTintColor={C.track}
        />
        <Text style={styles.paramHint}>男转女 +12，女转男 -12</Text>

        <View style={[styles.paramHead, { marginTop: 16 }]}>
          <Text style={styles.paramLabel}>特征检索比率</Text>
          <Text style={styles.paramValue}>{indexRate.toFixed(2)}</Text>
        </View>
        <Slider
          minimumValue={0}
          maximumValue={1}
          step={0.05}
          value={indexRate}
          onValueChange={setIndexRate}
          minimumTrackTintColor={C.tint}
          maximumTrackTintColor={C.track}
        />
        <Text style={styles.paramHint}>越高音色越准，但可能引入电音伪影</Text>

        <View style={[styles.paramHead, { marginTop: 16 }]}>
          <Text style={styles.paramLabel}>降噪</Text>
          <Switch value={denoise} onValueChange={setDenoise} trackColor={{ true: C.tint }} />
        </View>
      </Card>

      {/* 提交 */}
      <Button
        title={converting ? "转换中…" : "开始变声"}
        onPress={submit}
        disabled={!recUri || !voiceId || converting}
        loading={converting}
        style={{ marginTop: 20 }}
      />
      {err ? <Text style={styles.err}>{err}</Text> : null}
      {taskRunning && (
        <Card style={{ marginTop: 12 }}>
          <Text style={styles.convText}>{result?.message || "PC 端转换中…"}</Text>
        </Card>
      )}

      {/* 结果 */}
      {result?.status === "done" && result.url && (
        <Card style={{ marginTop: 16 }}>
          <View style={styles.rowBetween}>
            <Text style={styles.cardTitle}>变声结果</Text>
            <Badge text={`${result.duration_s.toFixed(1)}s`} tone="ok" />
          </View>
          <Row label="音色" value={result.voice_id} />
          <View style={{ marginTop: 14, alignItems: "center" }}>
            <View style={{ flexDirection: "row", gap: 14, alignItems: "center" }}>
              <PlayButton url={mediaUrl(result.url)} size={64} />
              <ShareButton
                path={result.url}
                name={`vc_${result.voice_id}_${Date.now()}.wav`}
                size={48}
              />
            </View>
            <Text style={styles.recText}>点击播放 · ↗ 保存/分享到手机</Text>
          </View>
        </Card>
      )}

      <Text style={styles.hint}>离线变声与级联变声、实时变声互斥（避免争抢显卡）。录音建议近距 15-20cm、安静环境。</Text>
    </ScrollView>
  );
}

const styles = StyleSheet.create({
  root: { flex: 1, backgroundColor: C.bg },
  body: { padding: 16, paddingTop: 12, paddingBottom: 40 },
  title: { fontSize: 24, fontWeight: "800", color: C.text, marginTop: 12 },
  subtitle: { fontSize: 13, color: C.sub, marginTop: 4, marginBottom: 16 },
  section: { fontSize: 13, fontWeight: "600", color: C.sub, marginBottom: 8, marginTop: 20, letterSpacing: 0.5 },
  cardTitle: { fontSize: 16, fontWeight: "700", color: C.text },
  rowBetween: { flexDirection: "row", justifyContent: "space-between", alignItems: "center" },
  recBtn: {
    width: 88,
    height: 88,
    borderRadius: 44,
    backgroundColor: C.tintSoft,
    alignItems: "center",
    justifyContent: "center",
  },
  recBtnActive: { backgroundColor: C.err },
  recText: { fontSize: 13, color: C.sub, marginTop: 12 },
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
  paramHead: { flexDirection: "row", justifyContent: "space-between", alignItems: "center", marginBottom: 4 },
  paramLabel: { fontSize: 14, fontWeight: "600", color: C.text },
  paramValue: { fontSize: 14, color: C.tint, fontWeight: "700", fontVariant: ["tabular-nums"] },
  paramHint: { fontSize: 11, color: C.sub, marginTop: 2 },
  convText: { fontSize: 13, color: C.sub, textAlign: "center" },
  err: { fontSize: 12, color: C.err, marginTop: 10, lineHeight: 17 },
  hint: { fontSize: 12, color: C.sub, marginTop: 16, lineHeight: 17 },
});
