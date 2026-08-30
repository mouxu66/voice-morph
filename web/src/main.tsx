import React from "react";
import ReactDOM from "react-dom/client";
import { HashRouter } from "react-router-dom";
import App from "./App";
import { applyTheme, getStoredTheme } from "./theme";

// 开源字体（SIL OFL，@fontsource 自托管：打进本地构建，离线可用）。
// tailwind.config 引用的三款字体此前从未实际加载，一直回退系统字体。
// 只取 latin 子集：本应用无希腊/西里尔/越南文内容，全量默认导入会多打
// ~700KB 无用字形进离线包。中文不受影响（拉丁字体不含汉字，中文走系统字体）。
import "@fontsource/outfit/latin-400.css";
import "@fontsource/outfit/latin-600.css";
import "@fontsource/outfit/latin-700.css";
import "@fontsource/inter/latin-400.css";
import "@fontsource/inter/latin-500.css";
import "@fontsource/inter/latin-600.css";
import "@fontsource/jetbrains-mono/latin-400.css";
import "@fontsource/jetbrains-mono/latin-500.css";
import "./styles/index.css";

// 渲染前先套用主题，避免亮/暗闪烁
applyTheme(getStoredTheme());

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <HashRouter>
      <App />
    </HashRouter>
  </React.StrictMode>
);
