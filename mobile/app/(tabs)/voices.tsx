import React, { useState } from "react";
import { FlatList, RefreshControl, StyleSheet, Text, View } from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";

import { RvcVoice, VoiceInfo, listRvcVoices, listVoices, mediaUrl } from "@/src/api";
import { Badge, C, Card, PlayButton, usePolling } from "@/src/ui";

type Item = {
  voice: VoiceInfo;
  rvc?: RvcVoice;
};

export default function VoicesScreen() {
  const insets = useSafeAreaInsets();
  const [items, setItems] = useState<Item[]>([]);
  const [refreshing, setRefreshing] = useState(false);
  const [loaded, setLoaded] = useState(false);

  const load = async () => {
    const [voices, rvc] = await Promise.all([
      listVoices().catch(() => [] as VoiceInfo[]),
      listRvcVoices().catch(() => null),
    ]);
    const rvcMap = new Map<string, RvcVoice>((rvc?.voices ?? []).map((v) => [v.id, v]));
    setItems(voices.map((v) => ({ voice: v, rvc: rvcMap.get(v.id) })));
    setLoaded(true);
  };

  usePolling(
    async () => {
      try {
        await load();
      } catch {
        /* ignore */
      }
    },
    30000,
    true
  );

  const onRefresh = async () => {
    setRefreshing(true);
    try {
      await load();
    } finally {
      setRefreshing(false);
    }
  };

  return (
    <View style={styles.root}>
      <View style={[styles.header, { paddingTop: insets.top + 12 }]}>
        <Text style={styles.title}>音色库</Text>
        <Text style={styles.subtitle}>{items.length} 个音色 · 下拉刷新</Text>
      </View>
      <FlatList
        data={items}
        keyExtractor={(x) => x.voice.id}
        contentContainerStyle={{ padding: 16, paddingBottom: 40 }}
        refreshControl={<RefreshControl refreshing={refreshing} onRefresh={onRefresh} />}
        renderItem={({ item }) => (
          <Card style={{ marginBottom: 12 }}>
            <View style={styles.rowBetween}>
              <View style={{ flex: 1, marginRight: 12 }}>
                <Text style={styles.name} numberOfLines={1}>
                  {item.voice.display_name || item.voice.id}
                </Text>
                <View style={styles.badgeRow}>
                  {item.voice.kind === "finetuned" ? (
                    <Badge text="微调" tone="tint" />
                  ) : (
                    <Badge text="克隆" tone="gray" />
                  )}
                  {item.rvc?.model_ready ? <Badge text="RVC 就绪" tone="ok" /> : null}
                  {item.rvc?.qc ? (
                    <Badge
                      text={`QC ${item.rvc.qc.score}${item.rvc.qc.pass ? "" : " 未过"}`}
                      tone={item.rvc.qc.pass ? "ok" : "warn"}
                    />
                  ) : null}
                  <Text style={styles.meta}>{item.voice.duration_s.toFixed(1)}s 参考音频</Text>
                </View>
              </View>
              {item.voice.reference ? <PlayButton url={mediaUrl(item.voice.reference)} size={44} /> : null}
            </View>
          </Card>
        )}
        ListEmptyComponent={
          loaded ? (
            <Text style={styles.empty}>暂无音色。请先在 PC 端入库音色。</Text>
          ) : (
            <Text style={styles.empty}>加载中…</Text>
          )
        }
      />
    </View>
  );
}

const styles = StyleSheet.create({
  root: { flex: 1, backgroundColor: C.bg },
  header: { paddingHorizontal: 16, paddingTop: 12, paddingBottom: 8, backgroundColor: C.bg },
  title: { fontSize: 24, fontWeight: "800", color: C.text },
  subtitle: { fontSize: 12, color: C.sub, marginTop: 4 },
  rowBetween: { flexDirection: "row", justifyContent: "space-between", alignItems: "center" },
  name: { fontSize: 16, fontWeight: "700", color: C.text },
  badgeRow: { flexDirection: "row", alignItems: "center", gap: 6, marginTop: 8 },
  meta: { fontSize: 11, color: C.sub, marginLeft: 2 },
  empty: { textAlign: "center", color: C.sub, marginTop: 40, fontSize: 13 },
});
