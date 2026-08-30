import { Tabs } from "expo-router";
import React from "react";
import { Ionicons } from "@expo/vector-icons";
import { useSafeAreaInsets } from "react-native-safe-area-context";

import { C } from "@/src/ui";

export default function TabLayout() {
  const insets = useSafeAreaInsets();
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
          tabBarIcon: ({ color }) => <Ionicons size={24} name="mic-outline" color={color} />,
        }}
      />
      <Tabs.Screen
        name="tts"
        options={{
          title: "合成",
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
