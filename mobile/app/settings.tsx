import { Ionicons } from "@expo/vector-icons";
import { router } from "expo-router";
import React, { useState } from "react";
import { Pressable, ScrollView, StyleSheet, Text, TextInput, View } from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";

import { applyAudioConfig, getHealth, restoreAudioConfig } from "@/src/api";
import { useAppStore } from "@/src/store";
import { Badge, Button, C, Card } from "@/src/ui";

export default function SettingsScreen() {
  const insets = useSafeAreaInsets();
  const { host, apiKey, setHost, setApiKey } = useAppStore();
  const [draft, setDraft] = useState(host);
  const [draftKey, setDraftKey] = useState(apiKey);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<"ok" | "fail" | null>(null);
  const [cuda, setCuda] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [msg, setMsg] = useState("");

  const normalize = (s: string) => s.trim().replace(/\/+$/, "");

  const test = async () => {
    setTesting(true);
    setTestResult(null);
    try {
      const h = normalize(draft);
      const headers: Record<string, string> = {};
      const k = draftKey.trim();
      if (k) headers["X-API-Key"] = k;
      const res = await fetch(`${h}/api/health`, { method: "GET", headers });
      if (res.ok) {
        const j = await res.json();
        setCuda(!!j.cuda);
        setTestResult("ok");
      } else if (res.status === 401) {
        setTestResult("fail");
        setMsg("后端要求鉴权：请填写正确的 API Key（与 PC 端 VM_API_TOKEN 一致）");
      } else {
        setTestResult("fail");
      }
    } catch {
      setTestResult("fail");
    } finally {
      setTesting(false);
    }
  };

  const save = () => {
    const h = normalize(draft);
    if (!/^https?:\/\/.+/.test(h)) {
      setMsg("地址需以 http:// 或 https:// 开头");
      return;
    }
    setHost(h);
    setApiKey(draftKey.trim());
    setMsg("已保存");
  };

  const doAudio = async (kind: "apply" | "restore") => {
    setBusy(kind);
    setMsg("");
    try {
      const r = kind === "apply" ? await applyAudioConfig() : await restoreAudioConfig();
      setMsg(r.ok ? (kind === "apply" ? "已应用最优音频配置" : "已恢复默认音频配置") : r.error || "操作失败");
    } catch (e) {
      setMsg(e instanceof Error ? e.message : "操作失败");
    } finally {
      setBusy(null);
    }
  };

  return (
    <View style={styles.root}>
      <View style={[styles.topbar, { paddingTop: insets.top + 12 }]}>
        <Pressable onPress={() => router.back()} style={styles.back}>
          <Ionicons name="chevron-back" size={24} color={C.text} />
        </Pressable>
        <Text style={styles.title}>设置</Text>
        <View style={{ width: 40 }} />
      </View>

      <ScrollView contentContainerStyle={styles.body}>
        {/* 后端地址 */}
        <Card>
          <Text style={styles.cardTitle}>PC 后端地址</Text>
          <Text style={styles.hint}>手机与电脑需在同一 WiFi。地址形如 http://192.168.1.10:8000</Text>
          <TextInput
            value={draft}
            onChangeText={setDraft}
            placeholder="http://192.168.1.10:8000"
            placeholderTextColor={C.sub}
            autoCapitalize="none"
            autoCorrect={false}
            keyboardType="url"
            style={styles.input}
          />
          <View style={{ flexDirection: "row", gap: 10, marginTop: 12 }}>
            <Button title={testing ? "测试中…" : "测试连接"} onPress={test} tone="ghost" loading={testing} style={{ flex: 1 }} />
            <Button title="保存" onPress={save} style={{ flex: 1 }} />
          </View>
          {testResult === "ok" && (
            <View style={styles.testOk}>
              <Badge text="连接成功" tone="ok" />
              {cuda ? <Text style={styles.cudaText}>CUDA 可用</Text> : <Text style={styles.hintInline}>无 CUDA（CPU 模式）</Text>}
            </View>
          )}
          {testResult === "fail" && <Text style={styles.err}>连接失败：检查电脑端应用是否已启动、地址是否正确、防火墙是否放行 8000 端口</Text>}
          {msg ? <Text style={styles.msg}>{msg}</Text> : null}
        </Card>

        {/* API Key（可选） */}
        <Card>
          <Text style={styles.cardTitle}>API Key（可选）</Text>
          <Text style={styles.hint}>
            局域网鉴权。PC 端以 VM_API_TOKEN 环境变量启动后端后，这里填相同值即可访问；不设则留空。PC 本机访问不受影响。
          </Text>
          <TextInput
            value={draftKey}
            onChangeText={setDraftKey}
            placeholder="留空 = 不鉴权"
            placeholderTextColor={C.sub}
            autoCapitalize="none"
            autoCorrect={false}
            secureTextEntry
            style={styles.input}
          />
        </Card>

        {/* PC 端音频设备 */}
        <Text style={styles.section}>PC 端音频设备</Text>
        <Card>
          <Text style={styles.hint}>
            一键最优：把 PC 的默认播放/录制设备切到实体声卡（Senary Audio），避免虚拟设备占用。变声结束后可恢复。
          </Text>
          <View style={{ flexDirection: "row", gap: 10, marginTop: 12 }}>
            <Button title={busy === "apply" ? "应用中…" : "一键最优"} onPress={() => doAudio("apply")} tone="ghost" loading={busy === "apply"} style={{ flex: 1 }} />
            <Button title={busy === "restore" ? "恢复中…" : "恢复默认"} onPress={() => doAudio("restore")} tone="ghost" loading={busy === "restore"} style={{ flex: 1 }} />
          </View>
        </Card>

        {/* 使用说明 */}
        <Text style={styles.section}>使用说明</Text>
        <Card>
          <Text style={styles.bullet}>• 遥控页：远程启动/停止 PC 端级联变声与 RVC 实时变声，实时查看转写文字与延迟统计</Text>
          <Text style={styles.bullet}>• 变声页：手机录音上传 PC 做 RVC 离线变声，结果回手机播放</Text>
          <Text style={styles.bullet}>• 合成页：输入文字，用音色库克隆合成语音</Text>
          <Text style={styles.bullet}>• 三种 GPU 任务互斥，PC 端有任务运行时其他任务会被拒绝（409）</Text>
        </Card>
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
  title: { fontSize: 18, fontWeight: "700", color: C.text },
  back: { padding: 8 },
  body: { padding: 16, paddingBottom: 40 },
  section: { fontSize: 13, fontWeight: "600", color: C.sub, marginBottom: 8, marginTop: 20, letterSpacing: 0.5 },
  cardTitle: { fontSize: 16, fontWeight: "700", color: C.text },
  hint: { fontSize: 12, color: C.sub, marginTop: 6, lineHeight: 17 },
  hintInline: { fontSize: 12, color: C.sub },
  input: {
    borderWidth: 1,
    borderColor: C.border,
    borderRadius: 10,
    padding: 12,
    fontSize: 14,
    color: C.text,
    backgroundColor: "#F8FAFB",
    marginTop: 10,
  },
  testOk: { flexDirection: "row", alignItems: "center", gap: 10, marginTop: 12 },
  cudaText: { fontSize: 12, color: C.ok, fontWeight: "600" },
  err: { fontSize: 12, color: C.err, marginTop: 10, lineHeight: 17 },
  msg: { fontSize: 12, color: C.tint, marginTop: 10, fontWeight: "600" },
  bullet: { fontSize: 13, color: C.text, lineHeight: 22 },
});
