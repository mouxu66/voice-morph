import { useCallback, useEffect, useRef, useState } from "react";
import {
  FtStatus,
  FtTrainStatus,
  ftAudition,
  ftDelete,
  ftPublish,
  ftTrain,
  ftUpload,
  getFtStatus,
  getFtTrainStatus,
} from "@/api/client";

/** 朗读稿：覆盖常用音素组合的短句，正常语速读完约 12~15 分钟 */
export const SCRIPT_SENTENCES: string[] = [
  "今天天气真不错，适合出门散步。",
  "我们的产品在市场上获得了广泛的好评。",
  "请把这份文件交给王经理，谢谢。",
  "科技改变生活，创新引领未来。",
  "记得明天早上八点在会议室开会。",
  "这道菜的做法其实非常简单。",
  "我最近在学习一门新的编程语言。",
  "窗外的雨下了整整一个下午。",
  "健康的生活方式需要坚持运动。",
  "这本书我看了三遍，每次都有新的收获。",
  "请问去火车站怎么走最近？",
  "孩子们在操场上快乐地奔跑。",
  "一杯咖啡，一本好书，一个安静的下午。",
  "团队合作是项目成功的关键。",
  "春天来了，公园里的花都开了。",
  "他的演讲赢得了热烈的掌声。",
  "我喜欢在清晨听鸟儿的叫声。",
  "这个问题的答案比想象中简单。",
  "旅行让人见识到世界的广阔。",
  "每天睡前读半小时书是个好习惯。",
  "新来的同事工作特别认真。",
  "周末我们全家去了郊外野餐。",
  "学无止境，每天都有新的知识等着我们。",
  "医生建议我多喝水多休息。",
  "这部电影情节紧凑，值得一看。",
  "秋风起，落叶铺满了小路。",
  "他把房间收拾得干干净净。",
  "人工智能正在改变我们的工作方式。",
  "奶奶做的饺子是我最爱的味道。",
  "保持好奇心，你会学到更多。",
  "音乐能够抚慰人的心灵。",
  "会议推迟到了下周三下午。",
  "登山是最好的减压方式之一。",
  "她画的风景画栩栩如生。",
  "这座城市的夜晚灯火辉煌。",
  "坚持每天写日记的人不多。",
  "快递员辛苦地把包裹送到每家每户。",
  "学习方法比努力程度更重要。",
  "冬天的第一场雪总是让人兴奋。",
  "老师耐心地解答了每个问题。",
  "市场的蔬菜新鲜又便宜。",
  "我们约定周五晚上一起吃饭。",
  "阅读能带我们去远方。",
  "小狗看到主人回家摇着尾巴迎上去。",
  "这个季节的橘子特别甜。",
  "时间管理是职场必修课。",
  "他修好了那台老旧的收音机。",
  "海边的日出美得让人屏住呼吸。",
  "大家齐心协力完成了任务。",
  "博物馆里陈列着许多珍贵的文物。",
  "小孩子总有问不完的为什么。",
  "一场大雨洗去了夏日的燥热。",
  "她轻轻地关上了房门。",
  "毕业典礼上大家都流下了眼泪。",
  "最新的研究表明运动能提高记忆力。",
  "爷爷在院子里种满了月季花。",
  "电话那头传来了熟悉的声音。",
  "认真倾听是一种难得的美德。",
  "这列火车开往北方的城市。",
  "做饭的时候记得打开抽油烟机。",
  "他的字写得工整又漂亮。",
  "一群大雁排成人字形往南飞。",
  "博物馆的门票需要提前预约。",
  "图书馆里安静得能听到翻书声。",
  "这个应用的操作界面非常友好。",
  "坚持早起一个月后，我精神好多了。",
  "朋友们聚在一起总有说不完的话。",
  "夜空中的星星一闪一闪的。",
  "他把失败当成了最好的老师。",
  "清晨的露珠挂在草叶上。",
  "每次遇到困难，她都很冷静。",
  "这条老街保留了许多传统建筑。",
  "运动之前要做好热身准备。",
  "新学期的课本已经发下来了。",
  "出租车司机热情地介绍了当地美食。",
  "演讲比赛吸引了三十多名选手。",
  "他每天练习钢琴一个小时。",
  "适当分享能增进彼此的信任。",
  "山顶的风景果然不负期待。",
  "这只猫喜欢趴在窗台上晒太阳。",
  "制定计划让工作更有效率。",
  "雨后的空气格外清新。",
  "他们球队赢得了决赛的胜利。",
  "老屋的木门发出吱呀的响声。",
  "技能的掌握离不开反复练习。",
  "她为新房子挑选了暖色的窗帘。",
  "公交车准时驶入了站台。",
  "这个问题我们下次再详细讨论。",
  "孩子们围坐着听爷爷讲故事。",
  "一杯热茶驱散了冬日的寒意。",
  "他的建议帮了我们大忙。",
  "周末的菜市场热闹非凡。",
  "经过整修，公园焕然一新。",
  "候车室里坐满了旅客。",
  "信心是成功的第一步。",
  "这家小店的面条非常有名。",
  "她用相机记录下旅途中的美好瞬间。",
  "团队成员来自五湖四海。",
  "正确的心态比天赋更重要。",
  "毕业后他选择回到家乡创业。",
  "办公室的绿植长得郁郁葱葱。",
  "讲座持续了两个小时，座无虚席。",
];

export type FtAuditionResult = { tuned_url: string; xvec_url?: string };

export function useFt() {
  const [voiceId, setVoiceId] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [status, setStatus] = useState<FtStatus | null>(null);
  const [trainStatus, setTrainStatus] = useState<FtTrainStatus | null>(null);
  const [audition, setAudition] = useState<FtAuditionResult | null>(null);
  const [auditionText, setAuditionText] = useState("这是一段用来对比微调效果的试听语音，感谢你的收听。");
  const [auditionBusy, setAuditionBusy] = useState(false);
  const [publishName, setPublishName] = useState("");
  const [publishing, setPublishing] = useState(false);
  const [publishOk, setPublishOk] = useState(false);
  const [error, setError] = useState("");
  const [recording, setRecording] = useState(false);
  const [recSeconds, setRecSeconds] = useState(0);
  const [uploading, setUploading] = useState(false);
  const [sentenceIdx, setSentenceIdx] = useState(0);
  const recorderRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const timerRef = useRef<number | null>(null);

  // 轮询处理/训练状态
  useEffect(() => {
    if (!voiceId) return;
    let alive = true;
    const poll = async () => {
      try {
        const st = await getFtStatus(voiceId);
        if (alive) setStatus(st);
        if (st.stage === "training" || st.stage === "trained" || st.stage === "published") {
          const ts = await getFtTrainStatus(voiceId);
          if (alive) setTrainStatus(ts);
        }
      } catch { /* 后端未就绪时静默 */ }
    };
    void poll();
    const t = window.setInterval(() => void poll(), 2000);
    return () => { alive = false; window.clearInterval(t); };
  }, [voiceId]);

  const startRecording = useCallback(async () => {
    setError("");
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: false },
      });
      const rec = new MediaRecorder(stream);
      chunksRef.current = [];
      rec.ondataavailable = (e) => { if (e.data.size > 0) chunksRef.current.push(e.data); };
      rec.onstop = () => {
        stream.getTracks().forEach((t) => t.stop());
        if (timerRef.current) window.clearInterval(timerRef.current);
      };
      rec.start(1000);
      recorderRef.current = rec;
      setRecording(true);
      setRecSeconds(0);
      timerRef.current = window.setInterval(() => setRecSeconds((s) => s + 1), 1000);
    } catch (e) {
      setError(`无法访问麦克风：${e instanceof Error ? e.message : String(e)}`);
    }
  }, []);

  const stopRecording = useCallback(async () => {
    const rec = recorderRef.current;
    if (!rec) return;
    const done = new Promise<void>((resolve) => { rec.onstop = () => resolve(); });
    rec.stop();
    await done;
    setRecording(false);
    const blob = new Blob(chunksRef.current, { type: rec.mimeType || "audio/webm" });
    if (blob.size < 100_000) {
      setError("录音太短（不足几秒），请重新录制");
      return;
    }
    setUploading(true);
    try {
      await ftUpload(voiceId, blob, "recording.webm");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setUploading(false);
    }
  }, [voiceId]);

  const uploadFile = useCallback(async (file: File) => {
    setError("");
    setUploading(true);
    try {
      await ftUpload(voiceId, file, file.name);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setUploading(false);
    }
  }, [voiceId]);

  const startTrain = useCallback(async (epochs: number) => {
    setError("");
    try {
      await ftTrain(voiceId, epochs);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [voiceId]);

  const doAudition = useCallback(async () => {
    setAuditionBusy(true);
    setError("");
    try {
      setAudition(await ftAudition(voiceId, auditionText));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setAuditionBusy(false);
    }
  }, [voiceId, auditionText]);

  const doPublish = useCallback(async () => {
    setPublishing(true);
    setError("");
    try {
      await ftPublish(voiceId, publishName || displayName || voiceId);
      setPublishOk(true);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setPublishing(false);
    }
  }, [voiceId, publishName, displayName]);

  const removeFt = useCallback(async (id: string) => {
    try { await ftDelete(id); } catch { /* ignore */ }
    if (id === voiceId) { setVoiceId(""); setStatus(null); setTrainStatus(null); setPublishOk(false); setAudition(null); }
  }, [voiceId]);

  const resetFlow = useCallback(() => {
    setVoiceId(""); setStatus(null); setTrainStatus(null);
    setAudition(null); setPublishOk(false); setError(""); setRecSeconds(0);
  }, []);

  return {
    voiceId, setVoiceId, displayName, setDisplayName,
    status, trainStatus, error,
    recording, recSeconds, uploading, sentenceIdx, setSentenceIdx,
    startRecording, stopRecording, uploadFile,
    startTrain, audition, auditionText, setAuditionText, auditionBusy, doAudition,
    publishName, setPublishName, publishing, publishOk, setPublishOk, doPublish,
    removeFt, resetFlow,
  };
}
