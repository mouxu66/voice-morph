"""通过 CDP 在桌宠页面里执行 JS 并取回结果（只读诊断用）。

用法：
    python tools/cdp_eval.py '<js 表达式>'
不传表达式时，输出桌宠面板的关键状态快照（状态栏文案 / 类名、按钮文案、
window.pet 暴露的方法名）。

为什么需要它：桌宠的发送结果靠主进程 `pet:send-result` 回传，一旦那条链路
断了，界面会**静默卡在"合成中"**——只能从渲染进程内部才看得出真实状态。
2026-09-18 那次"合成完了还写合成中"就是靠它定性的。
"""
import json
import sys
import urllib.request

from websockets.sync.client import connect

CDP = "http://127.0.0.1:9222/json"

SNAPSHOT = r"""
JSON.stringify({
  statusText: (document.getElementById('statusText')||{}).textContent,
  statusClass: (document.getElementById('status')||{}).className,
  sendLabel: (document.getElementById('sendLabel')||{}).textContent,
  voiceSel: (document.getElementById('voiceSel')||{}).value,
  recent: (document.getElementById('recent')||{}).textContent,
  hasPet: !!window.pet,
  petKeys: window.pet ? Object.keys(window.pet) : [],
  hasSendText: !!(window.pet && typeof window.pet.sendText === 'function'),
  hasOnSendResult: !!(window.pet && typeof window.pet.onSendResult === 'function')
})
"""


def main() -> None:
    expr = sys.argv[1] if len(sys.argv) > 1 else SNAPSHOT
    targets = json.load(urllib.request.urlopen(CDP, timeout=5))
    pet = next((t for t in targets if t.get("title") == "桌宠"), None)
    if not pet:
        print("找不到桌宠页面；当前页面：", [t.get("title") for t in targets])
        sys.exit(1)
    with connect(pet["webSocketDebuggerUrl"], open_timeout=5) as ws:
        ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate",
                            "params": {"expression": expr, "returnByValue": True}}))
        resp = json.loads(ws.recv())
    res = resp.get("result", {}).get("result", {})
    val = res.get("value")
    if isinstance(val, str):
        try:
            print(json.dumps(json.loads(val), ensure_ascii=False, indent=2))
            return
        except json.JSONDecodeError:
            pass
    print(json.dumps(res, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
