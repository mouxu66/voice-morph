// 桌宠触发的业务动作：发微信语音 / 试听 / 音色挖掘 / 实时变声开关等。
// 从 main.cjs 拆出，行为保持一致；纯动作层 —— 依赖 pet.cjs 的导览展示、
// alt-hint.cjs 的置顶横幅、backend.cjs 的 HTTP 工具。
const http = require("http");
const {
  BACKEND_PORT,
  backendPost,
  httpJson,
} = require("./backend.cjs");
const {
  showAltHint,
  hideAltHint,
  runAltHintCountdown,
  runManualPressGuide,
} = require("./alt-hint.cjs");
const {
  CAPTURE_SECONDS,
  getPetWin,
  showPetGuide,
  petGuideFail,
} = require("./pet.cjs");

/**
 * 发送微信语音消息的公共尾部：调 /api/wechat/play_to_cable（切麦克风→CABLE Output、
 * 播放 wav 到 CABLE Input、还原声卡）。不模拟 Alt 键 —— 由用户自己在微信里按 Alt 录。
 * 旧 send_voice 会自动模拟 Alt，但软件模拟的 Alt 在很多微信版本/环境下不触发录音，
 * 导致静默"成功"而微信什么也没收到。现在老老实实告诉用户"音频已放到 CABLE，请自己按 Alt"。
 * @param {string|null} wavName       要播放的 wav 文件名（null=最近合成）
 * @param {number|null} knownDurationS 已知音频时长（发送前已合成则传，精确倒计时）
 */
function sendWechatWav(wavName, knownDurationS) {
  const LEAD_S = 1.5;   // 静音头：给用户足够时间切到微信并按住 Alt（太长会录进大段空白）
  // 立即启动置顶提示横幅（准备→按住 Alt→松开），无需等后端响应
  const finishAltHint = runAltHintCountdown(
    Number.isFinite(knownDurationS) ? knownDurationS : null,
    LEAD_S,
    null,
  );
  backendPost("/api/wechat/play_to_cable", { wav: wavName, lead_s: LEAD_S }, (data, code) => {
    const err = data.error || data.detail || `HTTP ${code}`;
    if (data.ok) {
      // 播放已完成 → 立即收尾提示「松开 Alt，已发送」
      if (finishAltHint) finishAltHint();
      showPetGuide({
        title: "已发送",
        lines: [
          `音频已播放到 CABLE（${data.duration_s || "?"}s，含 ${data.lead_s || LEAD_S}s 静音头）`,
          "如果微信没收到，请确认按住 Alt 录音时微信是前台",
        ],
        action: "play", motion: "work", duration: 9000,
      });
    } else {
      hideAltHint();
      petGuideFail(err);
    }
    const petWin = getPetWin();
    if (petWin) {
      petWin.webContents.send("pet:send-result",
        data.ok
          ? { ok: true, duration_s: data.duration_s, hint: data.hint }
          : { ok: false, error: String(err) });
    }
  }, 180000);
}

/** 桌宠快捷面板「试听」：只合成不发送，产物信息回传面板供播放。 */
function previewWechatTextFromPet(text, voiceId) {
  const petWin = getPetWin();
  if (!petWin || !text) return;
  showPetGuide({
    title: "试听",
    lines: ["先合一段给你听听～"],
    action: "think", motion: "work", duration: 6000,
  });
  backendPost("/api/tts", { text, text_language: "zh", voice_id: voiceId || "" }, (data, code) => {
    if (!data.ok || !data.url) {
      const err = (data.detail && String(data.detail)) || `HTTP ${code}`;
      petGuideFail(err);
      petWin.webContents.send("pet:preview-result", { ok: false, error: String(err) });
      return;
    }
    const wav = String(data.url).split("/").pop();
    petWin.webContents.send("pet:preview-result", {
      ok: true, wav, duration_s: data.duration_s,
      url: `http://127.0.0.1:${BACKEND_PORT}${data.url}`,
    });
    showPetGuide({
      title: "试听",
      lines: [`好了（${data.duration_s || "?"}秒），听听看`, "满意就点「发送」"],
      action: "play", motion: "nod", duration: 8000,
    });
  });
}

/** 桌宠「发送微信语音」：把 outputs/ 下最近一次 TTS 合成发出去。 */
function sendWechatVoiceFromPet() {
  if (!getPetWin()) return;
  showPetGuide({
    title: "微信语音",
    lines: ["我把最近的合成语音录进微信～", "会按住 Alt 录、松开就发，这几秒别动键鼠"],
    action: "think", motion: "work", duration: 8000,
  });
  sendWechatWav(null);
}

/** 桌宠「录当前声音→挖掘音色」：loopback 内录系统播出声（抖音/视频），自动解析+挖掘。 */
function captureMineFromPet() {
  showPetGuide({
    title: "音色挖掘",
    lines: [`接下来 ${CAPTURE_SECONDS} 秒，把想抓的声音播出来`, "直接抓系统声音，不用麦克风"],
    action: "listen", motion: "float", duration: CAPTURE_SECONDS * 1000,
  });
  backendPost("/api/capture/loopback", { seconds: CAPTURE_SECONDS, auto: true }, (data, code) => {
    if (code !== 200 || !data.ok) {
      showPetGuide({
        title: "音色挖掘",
        lines: [String(data.detail || "录制失败，看看后端起没起").slice(0, 60)],
        action: "error", motion: "shake", duration: 8000,
      });
      return;
    }
    if (data.auto === false) {
      showPetGuide({
        title: "已保存素材",
        lines: [String(data.note || "素材已存，去音色库手动挖掘"), ""],
        action: "think", motion: "work", duration: 8000,
      });
      return;
    }
    showPetGuide({
      title: "音色挖掘",
      lines: ["录好了！正在去人声、切片、挖掘候选", "完成后我叫你，稍等～"],
      action: "think", motion: "work", duration: 8000,
    });
    pollMineResult();
  }, CAPTURE_SECONDS * 1000 + 45000);
}

/** 轮询挖掘状态直到结束（或超时），结果用桌宠气泡播报。 */
function pollMineResult() {
  let done = false;
  const timer = setInterval(async () => {
    if (done) return;
    const st = await httpJson("GET", "/api/mine/state");
    if (!st || !st.json || st.json.running) return;
    done = true;
    clearInterval(timer);
    if (st.json.stage === "done") {
      const n = (st.json.clusters || []).length;
      showPetGuide({
        title: "挖掘完成",
        lines: n
          ? [`挖出 ${n} 个候选音色`, "去「音色库」页试听，满意就保存"]
          : ["没挖出候选：素材里可能没人声或太杂", "换段内容再试一次"],
        action: n ? "play" : "error", motion: n ? "nod" : "shake", duration: 10000,
      });
    } else if (st.json.stage === "error") {
      showPetGuide({
        title: "挖掘失败",
        lines: [String(st.json.message || "未知错误").slice(0, 60)],
        action: "error", motion: "shake", duration: 8000,
      });
    }
  }, 3000);
  setTimeout(() => { done = true; clearInterval(timer); }, 10 * 60 * 1000);
}

/**
 * 桌宠「手动发变声语音」：切微信录音到 CABLE + 确保实时变声运行，
 * 然后用户自己在微信里按住 Alt 说话、松开发送。全程不自动按键。
 */
function manualWechatFromPet() {
  if (!getPetWin()) return;
  showPetGuide({
    title: "变声发语音",
    lines: ["准备中：切微信录音 + 开实时变声…"],
    action: "build", motion: "work", duration: 8000,
  });
  showAltHint({
    stage: "prep",
    sub: "正在准备变声，马上就好，请先切到微信聊天窗口",
    remainS: 3,
  });
  backendPost("/api/wechat/manual_send", {}, (data, code) => {
    if (!data.ok) {
      hideAltHint();
      petGuideFail((data.error || data.detail) || `HTTP ${code}`);
      return;
    }
    showPetGuide({
      title: "变声发语音",
      lines: [String(data.hint || "微信录音已切好，变声运行中"),
              String(data.hint2 || "去微信按住 Alt 说话，说完松开就发出")],
      action: "listen", motion: "work", duration: 15000,
    });
    // 手动变声：按住 Alt 说话、说完松开发送，横幅给出持续倒计时引导
    runManualPressGuide(15);
  }, 90000);
}

/** 桌宠快捷面板：输入文字 → 先 TTS 合成（指定音色，空则用当前选中）→ 再录进微信。 */
function sendWechatTextFromPet(text, voiceId) {
  if (!getPetWin() || !text) return;
  // 发前探活：连不上后端立刻报错，别闷头转圈被误判成「卡死」
  // （历史上桌宠曾写死连 8011、而 8000 才是健康后端，导致请求永远挂起、前端一直转圈）
  http.get(
    { host: "127.0.0.1", port: BACKEND_PORT, path: "/api/health", timeout: 3000 },
    (res) => { res.resume(); doSendTextToWechat(text, voiceId); },
  ).on("error", () => petGuideFail("后端没连上（8000 端口未启动？）"))
   .on("timeout", function () { try { this.destroy(); } catch {} petGuideFail("后端没连上（8000 探活超时）"); });
}

/** 探活通过后真正发起「合成并发送」。 */
function doSendTextToWechat(text, voiceId) {
  showPetGuide({
    title: "微信语音",
    lines: [`合成中：「${text.slice(0, 12)}${text.length > 12 ? "…" : ""}」`],
    action: "think", motion: "work", duration: 6000,
  });
  // 全自动：TTS → RVC 换声 → 自动点微信语音按钮录制并发送，全程不需要人按 Alt。
  // （旧的 /api/tts + play_to_cable 是半自动，还要用户自己按住 Alt 录，已改掉）
  showAltHint({
    stage: "prep",
    sub: "合成 + 换声中，约 1~2 分钟… 完成后自动发到微信，<b>别动键鼠</b>",
    remainS: null, progress: -1,
  });
  backendPost("/api/wechat/send_text",
    { text, voice_id: voiceId || "", rvc_voice: "", pitch: 0, index_rate: 0.5 },
    (data, code) => {
      const err = data.error || data.detail || `HTTP ${code}`;
      if (!data.ok) {
        hideAltHint();
        petGuideFail(err);
        return;
      }
      hideAltHint();
      const outcome = data.outcome || "ok";
      showPetGuide({
        title: outcome === "ok" ? "已发送到微信 ✓" : "发送未成功",
        lines: [
          `音频 ${data.duration_s || "?"}s（${data.wav || ""}）`,
          outcome === "ok" ? "去微信看最新那条语音" : `结果：${outcome}`,
        ].concat((data.steps || []).slice(-3)),
        action: outcome === "ok" ? "play" : "error", motion: "work", duration: 9000,
      });
    }, 180000);   // TTS + RVC + 录音可能两分钟，超时给足
}

/** 桌宠快捷面板：实时变声开关（运行中→停止；否则启动，模型用当前实验）。 */
function toggleLiveFromPet() {
  http.get(
    { host: "127.0.0.1", port: BACKEND_PORT, path: "/api/rvc/live/status", timeout: 4000 },
    (res) => {
      let data = "";
      res.on("data", (c) => { data += c; });
      res.on("end", () => {
        let st = {};
        try { st = JSON.parse(data); } catch {}
        if (st.live_running) {
          backendPost("/api/rvc/live/stop", {}, (d, code) => {
            showPetGuide({
              title: "实时变声",
              lines: [d.ok ? "已停止变声" : `停止失败：${(d.detail || code || "").toString().slice(0, 30)}`],
              action: d.ok ? "idle" : "error", duration: 6000,
            });
          });
        } else {
          showPetGuide({
            title: "实时变声",
            lines: ["启动中，模型就绪要一会儿…"],
            action: "build", duration: 6000,
          });
          backendPost("/api/rvc/live/start", {}, (d, code) => {
            if (d.ok) {
              showPetGuide({
                title: "实时变声",
                lines: ["变声已开启！", "微信里把录音设备指向 CABLE Output 就能用"],
                action: "listen", duration: 8000,
              });
            } else {
              petGuideFail(d.detail || `HTTP ${code}`);
            }
          }, 180000);
        }
      });
    },
  ).on("error", () => petGuideFail("后端服务没连上"));
}

module.exports = {
  sendWechatWav,
  previewWechatTextFromPet,
  sendWechatVoiceFromPet,
  captureMineFromPet,
  manualWechatFromPet,
  sendWechatTextFromPet,
  toggleLiveFromPet,
};
