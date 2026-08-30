// 共享 UI：学术干净风（纸面背景 + 白卡片 + 细边框轻阴影）
import React, { useCallback, useEffect, useRef, useState } from "react";
import { ActivityIndicator, Pressable, StyleSheet, Text, View, ViewStyle } from "react-native";
import { Audio } from "expo-av";
import * as FileSystem from "expo-file-system/legacy";
import * as Sharing from "expo-sharing";

import { mediaUrl } from "./api";

export const C = {
  bg: "#F6F7F9",
  card: "#FFFFFF",
  border: "#E4E7EC",
  text: "#11181C",
  sub: "#687076",
  tint: "#0a7ea4",
  tintSoft: "#E3F2F8",
  ok: "#16a34a",
  okSoft: "#E7F6EC",
  warn: "#d97706",
  warnSoft: "#FBEEDC",
  err: "#dc2626",
  errSoft: "#FBE5E5",
  track: "#E9EDF1",
};

export function Card({ style, children }: { style?: ViewStyle; children: React.ReactNode }) {
  return <View style={[styles.card, style]}>{children}</View>;
}

export function SectionTitle({ children }: { children: React.ReactNode }) {
  return <Text style={styles.sectionTitle}>{children}</Text>;
}

export function Badge({
  text,
  tone = "gray",
}: {
  text: string;
  tone?: "gray" | "ok" | "warn" | "err" | "tint";
}) {
  const map = {
    gray: { fg: C.sub, bg: C.track },
    ok: { fg: C.ok, bg: C.okSoft },
    warn: { fg: C.warn, bg: C.warnSoft },
    err: { fg: C.err, bg: C.errSoft },
    tint: { fg: C.tint, bg: C.tintSoft },
  } as const;
  const t = map[tone];
  return (
    <View style={[styles.badge, { backgroundColor: t.bg }]}>
      <Text style={[styles.badgeText, { color: t.fg }]}>{text}</Text>
    </View>
  );
}

export function Button({
  title,
  onPress,
  tone = "primary",
  disabled,
  loading,
  style,
}: {
  title: string;
  onPress: () => void;
  tone?: "primary" | "danger" | "ghost";
  disabled?: boolean;
  loading?: boolean;
  style?: ViewStyle;
}) {
  const bg = tone === "primary" ? C.tint : tone === "danger" ? C.err : "transparent";
  const fg = tone === "ghost" ? C.tint : "#fff";
  return (
    <Pressable
      onPress={onPress}
      disabled={disabled || loading}
      style={({ pressed }) => [
        styles.btn,
        { backgroundColor: bg, opacity: disabled || loading ? 0.5 : pressed ? 0.85 : 1 },
        tone === "ghost" && { borderWidth: 1, borderColor: C.border, backgroundColor: "#fff" },
        style,
      ]}>
      {loading ? (
        <ActivityIndicator color={fg} size="small" />
      ) : (
        <Text style={[styles.btnText, { color: fg }]}>{title}</Text>
      )}
    </Pressable>
  );
}

export function StatPill({ label, value }: { label: string; value: string }) {
  return (
    <View style={styles.stat}>
      <Text style={styles.statValue}>{value}</Text>
      <Text style={styles.statLabel}>{label}</Text>
    </View>
  );
}

export function Row({ label, value }: { label: string; value: string }) {
  return (
    <View style={styles.row}>
      <Text style={styles.rowLabel}>{label}</Text>
      <Text style={styles.rowValue} numberOfLines={1}>
        {value}
      </Text>
    </View>
  );
}

/** 音频播放按钮：点击加载/播放/暂停，组件卸载自动释放 */
export function PlayButton({ url, size = 40 }: { url: string; size?: number }) {
  const [state, setState] = useState<"idle" | "loading" | "playing" | "paused" | "error">("idle");
  const soundRef = useRef<Audio.Sound | null>(null);

  useEffect(() => {
    return () => {
      soundRef.current?.unloadAsync();
      soundRef.current = null;
    };
  }, []);

  const toggle = useCallback(async () => {
    try {
      if (!soundRef.current) {
        setState("loading");
        const { sound } = await Audio.Sound.createAsync({ uri: url });
        soundRef.current = sound;
        sound.setOnPlaybackStatusUpdate((s) => {
          if (s.isLoaded && s.didJustFinish) setState("paused");
        });
        setState("playing");
        await sound.playAsync();
        return;
      }
      const s = soundRef.current;
      const st = await s.getStatusAsync();
      if (st.isLoaded && st.isPlaying) {
        await s.pauseAsync();
        setState("paused");
      } else {
        await s.playAsync();
        setState("playing");
      }
    } catch {
      setState("error");
    }
  }, [url]);

  const label = state === "playing" ? "⏸" : state === "error" ? "!" : "▶";
  const bg = state === "playing" ? C.tint : C.tintSoft;
  const fg = state === "playing" ? "#fff" : C.tint;
  return (
    <Pressable onPress={toggle} style={({ pressed }) => [styles.play, { width: size, height: size, borderRadius: size / 2, backgroundColor: bg, opacity: pressed ? 0.8 : 1 }]}>
      {state === "loading" ? <ActivityIndicator size="small" color={fg} /> : <Text style={{ color: fg, fontSize: size * 0.4, fontWeight: "700" }}>{label}</Text>}
    </Pressable>
  );
}

/** 分享按钮：把 PC 端音频经局域网下载到手机缓存，调起系统分享（微信/QQ/文件…）。
 *  path 是后端返回的相对音频路径；name 是落盘文件名（带扩展名）。 */
export function ShareButton({ path, name, size = 40 }: { path: string; name: string; size?: number }) {
  const [state, setState] = useState<"idle" | "downloading" | "error">("idle");

  const share = useCallback(async () => {
    try {
      setState("downloading");
      if (!(await Sharing.isAvailableAsync())) throw new Error("当前设备不支持系统分享");
      const url = mediaUrl(path);
      if (!url) throw new Error("音频地址为空");
      const dest = `${FileSystem.cacheDirectory}${name}`;
      const info = await FileSystem.getInfoAsync(dest);
      if (info.exists) await FileSystem.deleteAsync(dest, { idempotent: true });
      const res = await FileSystem.createDownloadResumable(url, dest).downloadAsync();
      if (!res || res.status < 200 || res.status >= 300) throw new Error(`下载失败（HTTP ${res?.status ?? "?"}）`);
      const ext = (name.split(".").pop() || "").toLowerCase();
      const mime = ext === "mp3" ? "audio/mpeg" : ext === "m4a" || ext === "mp4" ? "audio/mp4" : "audio/wav";
      await Sharing.shareAsync(dest, { mimeType: mime, dialogTitle: "分享音频" });
      setState("idle");
    } catch {
      setState("error");
    }
  }, [path, name]);

  const bg = state === "error" ? C.errSoft : C.tintSoft;
  const fg = state === "error" ? C.err : C.tint;
  return (
    <Pressable
      onPress={share}
      disabled={state === "downloading"}
      style={({ pressed }) => [
        styles.play,
        { width: size, height: size, borderRadius: size / 2, backgroundColor: bg, opacity: pressed ? 0.8 : 1 },
      ]}>
      {state === "downloading" ? (
        <ActivityIndicator size="small" color={fg} />
      ) : (
        <Text style={{ color: fg, fontSize: size * 0.42, fontWeight: "700" }}>{state === "error" ? "!" : "↗"}</Text>
      )}
    </Pressable>
  );
}

/** 轮询 hook：enabled 时按 interval 周期执行 fn，页面失焦自动继续（简单实现） */
export function usePolling(fn: () => Promise<void>, intervalMs: number, enabled: boolean) {
  const fnRef = useRef(fn);
  fnRef.current = fn;
  useEffect(() => {
    if (!enabled) return;
    let stopped = false;
    const tick = async () => {
      try {
        await fnRef.current();
      } catch {
        /* 轮询失败静默，由 UI 层展示状态 */
      }
      if (!stopped) setTimeout(tick, intervalMs);
    };
    tick();
    return () => {
      stopped = true;
    };
  }, [enabled, intervalMs]);
}

const styles = StyleSheet.create({
  card: {
    backgroundColor: C.card,
    borderRadius: 16,
    borderWidth: 1,
    borderColor: C.border,
    padding: 16,
    shadowColor: "#0B1220",
    shadowOpacity: 0.05,
    shadowRadius: 10,
    shadowOffset: { width: 0, height: 2 },
    elevation: 2,
  },
  sectionTitle: {
    fontSize: 13,
    fontWeight: "600",
    color: C.sub,
    marginBottom: 8,
    marginTop: 20,
    letterSpacing: 0.5,
  },
  badge: {
    borderRadius: 999,
    paddingHorizontal: 10,
    paddingVertical: 3,
    alignSelf: "flex-start",
  },
  badgeText: {
    fontSize: 12,
    fontWeight: "600",
  },
  btn: {
    borderRadius: 12,
    paddingVertical: 13,
    paddingHorizontal: 18,
    alignItems: "center",
    justifyContent: "center",
  },
  btnText: {
    fontSize: 15,
    fontWeight: "600",
  },
  stat: {
    flex: 1,
    backgroundColor: "#F8FAFB",
    borderRadius: 10,
    paddingVertical: 10,
    alignItems: "center",
  },
  statValue: {
    fontSize: 15,
    fontWeight: "700",
    color: C.text,
    fontVariant: ["tabular-nums"],
  },
  statLabel: {
    fontSize: 11,
    color: C.sub,
    marginTop: 2,
  },
  row: {
    flexDirection: "row",
    justifyContent: "space-between",
    alignItems: "center",
    paddingVertical: 4,
  },
  rowLabel: {
    fontSize: 13,
    color: C.sub,
  },
  rowValue: {
    fontSize: 13,
    color: C.text,
    fontWeight: "500",
    maxWidth: "65%",
  },
  play: {
    alignItems: "center",
    justifyContent: "center",
  },
});
