import { Tabs } from "expo-router";
import React from "react";
import { Ionicons } from "@expo/vector-icons";
import { useSafeAreaInsets } from "react-native-safe-area-context";

import { pluginVisible, useCapabilities } from "@/src/capabilities";
import { C } from "@/src/ui";

export default function TabLayout() {
  const insets = useSafeAreaInsets();
  const caps = useCapabilities();
  // 变声 tab 整体由 sound.offline-vc 支撑，合成 tab 由 sound.tts 支撑；
  // 清单拿到且明确关闭时隐藏对应 tab（拿不到目录 → 全部显示）。
  const vcVisible = pluginVisible(caps, "sound.offline-vc");
  const ttsVisible = pluginVisible(caps, "sound.tts");
  return (
    <Tabs
      screenOptions={{
        headerShown: false,
        tabBarActiveTintColor: C.tint,
        tabBarInactiveTintColor: C.sub,
        tabBarStyle: {
          backgroundColor: "#FFFFFF",
          borderTopColor: C.border,
          borderTopWidth: 1,
          height: 62 + insets.bottom,
          paddingBottom: insets.bottom + 6,
          paddingTop: 6,
        },
        tabBarLabelStyle: { fontSize: 11, fontWeight: "500" },
      }}>
      <Tabs.Screen
        name="index"
        options={{
          title: "遥控",
          tabBarIcon: ({ color }) => <Ionicons size={24} name="radio-outline" color={color} />,
        }}
      />
      <Tabs.Screen
        name="vc"
        options={{
          title: "变声",
          ...(vcVisible ? {} : { href: null }),
          tabBarIcon: ({ color }) => <Ionicons size={24} name="mic-outline" color={color} />,
        }}
      />
      <Tabs.Screen
        name="tts"
        options={{
          title: "合成",
          ...(ttsVisible ? {} : { href: null }),
          tabBarIcon: ({ color }) => <Ionicons size={24} name="text-outline" color={color} />,
        }}
      />
      <Tabs.Screen
        name="voices"
        options={{
          title: "音色库",
          tabBarIcon: ({ color }) => <Ionicons size={24} name="library-outline" color={color} />,
        }}
      />
    </Tabs>
  );
}
