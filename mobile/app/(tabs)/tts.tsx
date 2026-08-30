import React, { useEffect, useState } from "react";
import { Pressable, ScrollView, StyleSheet, Text, TextInput, View } from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";

import { TtsResult, VoiceInfo, listVoices, mediaUrl, sendTts } from "@/src/api";
import { useAppStore } from "@/src/store";
import { Badge, Button, C, Card, PlayButton, ShareButton, usePolling } from "@/src/ui";

export default function TtsScreen() {
  const insets = useSafeAreaInsets();
  const { lastVoiceId, setLastVoiceId } = useAppStore();
  const [text, setText] = useState("");
  const [lang, setLang] = useState<"zh" | "en">("zh");
  const [voices, setVoices] = useState<VoiceInfo[]>([]);
  const [voiceId, setVoiceId] = useState<string | null>(lastVoiceId);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [result, setResult] = useState<TtsResult | null>(null);
  const [elapsed, setElapsed] = useState(0);

  usePolling(
    async () => {
      try {
        const vs = await listVoices();
        setVoices(vs);
        if (!voiceId && vs.length > 0) {
          setVoiceId(vs[0].id);
          setLastVoiceId(vs[0].id);
        }
      } catch {
        /* ignore */
      }
    },
    30000,
    true
  );

  // 合成中计时
  useEffect(() => {
    if (!busy) return;
    setElapsed(0);
    const t = setInterval(() => setElapsed((e) => e + 0.1), 100);
    return () => clearInterval(t);
  }, [busy]);

  const submit = async () => {
    const t = text.trim();
    if (!t || !voiceId) return;
    setBusy(true);
    setErr("");
    setResult(null);
    try {
      const r = await sendTts(t, lang, voiceId);
      setResult(r);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "合成失败");
    } finally {
      setBusy(false);
    }
  };

  return (
    <ScrollView style={styles.root} contentContainerStyle={[styles.body, { paddingTop: insets.top + 12 }]} keyboardShouldPersistTaps="handled">
      <Text style={styles.title}>文字合成</Text>
      <Text style={styles.subtitle}>输入文字 → PC 端 Qwen3-TTS 克隆合成 → 回传播放</Text>

      {/* 文本输入 */}
      <TextInput
        value={text}
        onChangeText={setText}
        placeholder="输入要合成的话…"
        placeholderTextColor={C.sub}
        multiline
        style={styles.input}
      />

      {/* 语言切换 */}
      <View style={styles.langRow}>
        {(["zh", "en"] as const).map((l) => (
          <Pressable key={l} onPress={() => setLang(l)} style={[styles.chip, lang === l && styles.chipActive]}>
            <Text style={[styles.chipText, lang === l && styles.chipTextActive]}>{l === "zh" ? "中文" : "英文"}</Text>
          </Pressable>
        ))}
      </View>

      {/* 音色选择 */}
      <Text style={styles.section}>音色</Text>
      <ScrollView horizontal showsHorizontalScrollIndicator={false} contentContainerStyle={{ gap: 8 }}>
        {voices.map((v) => (
          <Pressable
            key={v.id}
            onPress={() => {
              setVoiceId(v.id);
              setLastVoiceId(v.id);
            }}
            style={[styles.chip, v.id === voiceId && styles.chipActive]}>
            <Text style={[styles.chipText, v.id === voiceId && styles.chipTextActive]}>{v.display_name || v.id}</Text>
          </Pressable>
        ))}
        {voices.length === 0 && <Text style={styles.hint}>暂无音色（PC 端未入库）</Text>}
      </ScrollView>

      {/* 合成 */}
      <Button
        title={busy ? `合成中 ${elapsed.toFixed(1)}s…` : "开始合成"}
        onPress={submit}
        disabled={!text.trim() || !voiceId || busy}
        loading={busy}
        style={{ marginTop: 20 }}
      />
      {err ? <Text style={styles.err}>{err}</Text> : null}

      {/* 结果 */}
      {result && (
        <Card style={{ marginTop: 16 }}>
          <View style={styles.rowBetween}>
            <Text style={styles.cardTitle}>合成结果</Text>
            <Badge text={`${result.duration_s.toFixed(1)}s`} tone="ok" />
          </View>
          <View style={{ marginTop: 14, alignItems: "center" }}>
            <View style={{ flexDirection: "row", gap: 14, alignItems: "center" }}>
              <PlayButton url={mediaUrl(result.url)} size={64} />
              <ShareButton path={result.url} name={`tts_${Date.now()}.wav`} size={48} />
            </View>
            <Text style={styles.playHint}>点击播放 · ↗ 保存/分享到手机</Text>
          </View>
        </Card>
      )}

      <Text style={styles.hint}>首次合成可能需要 10-20 秒（模型预热），之后同一音色会快很多。</Text>
    </ScrollView>
  );
}

const styles = StyleSheet.create({
  root: { flex: 1, backgroundColor: C.bg },
  body: { padding: 16, paddingBottom: 40 },
  title: { fontSize: 24, fontWeight: "800", color: C.text, marginTop: 12 },
  subtitle: { fontSize: 13, color: C.sub, marginTop: 4, marginBottom: 16 },
  input: {
    backgroundColor: "#fff",
    borderWidth: 1,
    borderColor: C.border,
    borderRadius: 14,
    padding: 14,
    fontSize: 15,
    color: C.text,
    minHeight: 120,
    textAlignVertical: "top",
  },
  langRow: { flexDirection: "row", gap: 8, marginTop: 12 },
  section: { fontSize: 13, fontWeight: "600", color: C.sub, marginBottom: 8, marginTop: 20, letterSpacing: 0.5 },
  cardTitle: { fontSize: 16, fontWeight: "700", color: C.text },
  rowBetween: { flexDirection: "row", justifyContent: "space-between", alignItems: "center" },
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
  playHint: { fontSize: 13, color: C.sub, marginTop: 12 },
  err: { fontSize: 12, color: C.err, marginTop: 10, lineHeight: 17 },
  hint: { fontSize: 12, color: C.sub, marginTop: 16, lineHeight: 17 },
});
