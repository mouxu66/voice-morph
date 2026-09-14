// 外部资源的「去哪下载」指引（纯数据，无副作用，便于测试与 IPC 直接回传）
//
// 解决的问题：安装包不含模型（约 20 GB，且多数权重有分发限制），干净机器上用户
// 面对的是一个空目录选择器 —— 他知道要选"RVC 整合包"，但不知道去哪拿、拿到什么样
// 才算对。本模块把那句话补全：**去哪下 → 下完长什么样 → 选哪一层**。
//
// 三条硬纪律（都是踩出来的）：
//   1. **链接必须实测可达**（`verifiedAt` 记录核对日期）。猜一个 404 的路径，
//      比不给链接更伤 —— 用户会以为是自己操作错了。
//   2. **上游目录名 ≠ 本应用要求的目录名**。Qwen 官方叫 `Qwen3-TTS-12Hz-1.7B-Base`，
//      本应用找的是 `qwen3-tts-1.7b-base`（见 config.py 的 QWEN_MODEL_DIR 推导）。
//      不把这一步写成可直接复制的命令，用户下完必然"配好了却仍报未配置"。
//   3. **体积要写**。20 GB 的下载不该让用户在点下去之后才知道。
const fs = require("fs");
const path = require("path");

/** 所有指引用到的链接都实测过可达性的日期 */
const VERIFIED_AT = "2026-09-14";

/** 上游（Qwen 官方）模型仓库与本应用要求的本地目录名的对应关系 —— 别改错。 */
const QWEN_BASE_REPO = "Qwen/Qwen3-TTS-12Hz-1.7B-Base";
const QWEN_TOK_REPO = "Qwen/Qwen3-TTS-Tokenizer-12Hz";
const QWEN_BASE_DIR = "qwen3-tts-1.7b-base";
const QWEN_TOK_DIR = "qwen3-tts-tokenizer-12hz";

const GUIDE_LIST = [
  {
    key: "tts_models",
    label: "Qwen3-TTS 模型",
    sizeText: "约 4.9 GB",
    why: "文字转语音、音色微调、微信一键发送都依赖它。缺了这些功能整块不可用。",
    layout: [
      "<你选的那一层目录>/",
      `  ${QWEN_BASE_DIR}/          ← 语言模型（4.3 GB）`,
      `  ${QWEN_TOK_DIR}/   ← 分词器（0.65 GB）`,
      "  （可选）ref/               ← 兜底参考音，放任意 .wav",
    ],
    steps: [
      {
        title: "1. 装下载工具（国内推荐 ModelScope）",
        command: "pip install -U modelscope",
      },
      {
        title: "2. 下两个模型到正确目录（两步都要做）",
        detail:
          "把 <项目根> 换成实际位置（本项目是 D:\\变声）。注意 `--local_dir` 里写的是" +
          "本应用要求的目录名，与上游仓库名不同 —— 直接照抄，别改成官方名。",
        command:
          "modelscope download --model " + QWEN_BASE_REPO + " --local_dir <项目根>\\tts_models\\" + QWEN_BASE_DIR + "\n" +
          "modelscope download --model " + QWEN_TOK_REPO + " --local_dir <项目根>\\tts_models\\" + QWEN_TOK_DIR,
      },
      {
        title: "3. 核对目录（下完应该长这样）",
        detail:
          "两个子目录里各有一个 model.safetensors；只下 1.7B-Base 而漏掉分词器，" +
          "合成会在加载期直接失败。",
      },
    ],
    links: [
      { label: "ModelScope · 1.7B-Base", url: `https://www.modelscope.cn/models/${QWEN_BASE_REPO}`, note: "国内直连" },
      { label: "ModelScope · Tokenizer", url: `https://www.modelscope.cn/models/${QWEN_TOK_REPO}`, note: "国内直连" },
      { label: "HuggingFace · 1.7B-Base", url: `https://huggingface.co/${QWEN_BASE_REPO}`, note: "需网络可达" },
      { label: "HuggingFace · Tokenizer", url: `https://huggingface.co/${QWEN_TOK_REPO}`, note: "需网络可达" },
      { label: "Qwen3-TTS 官方仓库", url: "https://github.com/QwenLM/Qwen3-TTS" },
    ],
    notes: [
      "用 `huggingface-cli download` 时**必须带 `--local-dir`**：省略它模型只会落进 " +
        "`~/.cache/huggingface`，本应用不扫那里，你会得到「下完了却仍报未配置」。",
      "上游仓库名带 `12Hz`、本地目录名不带 —— 不是笔误，本应用按本地名查找。",
    ],
  },
  {
    key: "tts_venv",
    label: "TTS 专用解释器",
    sizeText: "约 8 GB（主要是 torch）",
    why:
      "Qwen3-TTS 要 Python 3.12 + torch，与主后端（3.11）装同一个环境会冲突，" +
      "所以单开一个环境跑 TTS worker。",
    layout: [
      "<项目根>/",
      "  tts_trial/venv312/Scripts/python.exe   ← 面板里要选的就是这个文件",
    ],
    steps: [
      {
        title: "1. 建 Python 3.12 环境（放在项目根下）",
        detail: "需要本机已装 Python 3.12（`py -3.12 -V` 能出版本号）。",
        command:
          "cd <项目根>\n" +
          "py -3.12 -m venv tts_trial\\venv312\n" +
          "tts_trial\\venv312\\Scripts\\python.exe -m pip install -U pip",
      },
      {
        title: "2. 装 torch（必须带 CUDA 索引，否则装到 CPU 版会慢到不可用）",
        command:
          "tts_trial\\venv312\\Scripts\\python.exe -m pip install torch torchaudio " +
          "--index-url https://download.pytorch.org/whl/cu129",
      },
      {
        title: "3. 装 Qwen3-TTS 与加速包",
        command:
          "tts_trial\\venv312\\Scripts\\python.exe -m pip install -U " +
          "qwen-tts qwen-tts-hf faster-qwen3-tts",
      },
      {
        title: "4. 不管它也行",
        detail:
          "模型目录选对时，本应用会从 `<项目根>/tts_trial/venv312/Scripts/python.exe` " +
          "自动推导出这一项，通常不需要手动指定。",
      },
    ],
    links: [
      { label: "Qwen3-TTS 官方仓库", url: "https://github.com/QwenLM/Qwen3-TTS" },
      { label: "faster-qwen3-tts（加速包）", url: "https://github.com/andimarafioti/faster-qwen3-tts" },
      { label: "qwen-tts（PyPI）", url: "https://pypi.org/project/qwen-tts/" },
      { label: "PyTorch 安装索引", url: "https://pytorch.org/get-started/locally/" },
    ],
    notes: [
      "这一项**没有「下载一个包」的路子** —— 只能在本机构建。8 GB 里绝大部分是 torch，" +
        "慢是正常的，别中途关掉。",
      "若把环境建在别处也能用，只是面板里要手动指定 python.exe。",
    ],
  },
  {
    key: "rvc_root",
    label: "RVC 整合包",
    sizeText: "约 7.3 GB（压缩包）",
    why: "实时变声、离线 RVC 变声、音色训练都跑在它里面。TTS 与离线合成不依赖它。",
    layout: [
      "D:\\RVC/                 ← 面板里要选的就是这一层",
      "  assets/                ← 整合包自带底模，不用另下",
      "  infer/",
      "  logs/                  ← 训练出的音色权重落这里",
      "  go-webui.bat",
    ],
    steps: [
      {
        title: "1. 按显卡选完整包",
        detail:
          "RTX 50 系（含 5060 / 5070 / 5080 / 5090）选 `…Nvidia50x0.7z`；" +
          "其它 NVIDIA 卡选 `…Nvidia.7z`；AMD / Intel 卡选 `…AMD_Intel.7z`。选错的包" +
          "可能在推理时报 kernel 不兼容。",
      },
      {
        title: "2. 用 7-Zip 解压到短路径（建议 D:\\RVC）",
        detail:
          "解压后根目录应能看到 assets/ infer/ logs/ go-webui.bat 这几项 —— " +
          "面板里就选这一层，**不要**选到上一级或里层的 infer/。",
      },
      {
        title: "3. 不用启动它的 WebUI",
        detail:
          "本应用只把整合包当作推理环境调用，整合包自带 Python 运行时与全部底模，" +
          "**不需要**再跑 `go-webui.bat`，也不需要另外 `pip install`。",
      },
    ],
    links: [
      {
        label: "官方 Releases 页",
        url: "https://github.com/RVC-Project/Retrieval-based-Voice-Conversion-WebUI/releases/latest",
        note: "先看这里，版本会更新",
      },
      {
        label: "NVIDIA 50 系完整包（7.25 GB）",
        url: "https://huggingface.co/lj1995/VoiceConversionWebUI/resolve/main/RVC20260718Nvidia50x0.7z",
        note: "RTX 5060 用这个",
      },
      {
        label: "NVIDIA 其它型号完整包（7.44 GB）",
        url: "https://huggingface.co/lj1995/VoiceConversionWebUI/resolve/main/RVC20260718Nvidia.7z",
      },
      {
        label: "AMD / Intel 完整包（5.32 GB）",
        url: "https://huggingface.co/lj1995/VoiceConversionWebUI/resolve/main/RVC20260718AMD_Intel.7z",
      },
      { label: "整套资源目录（HF）", url: "https://huggingface.co/lj1995/VoiceConversionWebUI" },
    ],
    notes: [
      "上面的直链对应 release `2.3.260718`（2026-07-21）。直接给直链是为了省掉在 " +
        "release 正文里翻链接的功夫，但**版本会过期** —— 链接失效时回 Releases 页取当前版本。",
      "解压路径建议纯英文、无空格（如 `D:\\RVC`）。本应用在没配置时默认也按 `D:\\RVC` 查找。",
    ],
  },
];

/** key → guide */
const GUIDES = Object.fromEntries(GUIDE_LIST.map((g) => [g.key, g]));

/** 取某个 kind 的指引；未知 key 返回 null（调用方负责降级）。 */
function guideFor(kind) {
  return GUIDES[kind] || null;
}

/** 全部指引（IPC 一次性回传给渲染层，省得多轮往返） */
function allGuides() {
  // 深拷贝：渲染层拿到的是跨进程结构，返回内部引用容易被误改
  return JSON.parse(JSON.stringify(GUIDE_LIST));
}

/**
 * 按「kind + 链接序号」解析出待打开的 URL。
 *
 * 渲染层**永远不传 URL 字符串**，只传序号 —— 这样即使渲染层被注入，也无法让主进程
 * `shell.openExternal` 打开任意地址。返回值里的 `url` 只可能是本模块的常量。
 *
 * @returns {{ url: string, label: string } | null}
 */
function resolveLink(kind, index) {
  const g = guideFor(kind);
  if (!g) return null;
  // 必须是真正的 number：`Number("0x0")` 也是 0，只判 isInteger 会把字符串放进来
  // （本模块的调用方是 IPC，跨进程边界上的类型宽松是缺陷不是便利）
  if (typeof index !== "number" || !Number.isInteger(index)) return null;
  if (index < 0 || index >= g.links.length) return null;
  const link = g.links[index];
  return { url: link.url, label: link.label };
}

/** 该项在当前机器上是否只能手动构建（无现成下载物）—— 决定 UI 文案。 */
function isManualOnly(kind) {
  return kind === "tts_venv";
}

/** 指引里指向的某个文件是否已在本机（用于「你可能已经下过了，只是没解压」提示） */
function findDownloadedArchives(root, kind) {
  if (kind !== "rvc_root") return [];
  const hits = [];
  try {
    for (const name of fs.readdirSync(root)) {
      if (/\.7z$/i.test(name) && /rvc/i.test(name)) hits.push(path.join(root, name));
    }
  } catch {
    /* 无权限/不存在：当作没找到 */
  }
  return hits;
}

module.exports = {
  VERIFIED_AT,
  QWEN_BASE_REPO,
  QWEN_TOK_REPO,
  QWEN_BASE_DIR,
  QWEN_TOK_DIR,
  GUIDES,
  guideFor,
  allGuides,
  resolveLink,
  isManualOnly,
  findDownloadedArchives,
};
