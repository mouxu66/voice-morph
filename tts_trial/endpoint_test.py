# -*- coding: utf-8 -*-
"""对 Senary 扬声器端点做 WASAPI 共享模式初始化测试：输出混合格式与 HRESULT。
结果写 endpoint_test.txt。"""
import os

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "endpoint_test.txt")


def log(m):
    with open(OUT, "a", encoding="utf-8") as f:
        f.write(m + "\n")


open(OUT, "w").close()


def main():
    from comtypes import CLSCTX_ALL
    from pycaw.pycaw import AudioUtilities, IAudioClient
    # comtypes 在导入时已按默认套间初始化，这里不再重复 CoInit
    # 枚举全部渲染设备，找 扬声器(Senary)
    utils = AudioUtilities()
    for d in utils.GetAllDevices():
        try:
            name = d.FriendlyName
        except Exception:
            name = "?"
        flow = getattr(d, "_flow", None)
        log(f"[dev] {name} flow={flow}")
    dev = AudioUtilities.GetSpeakers()
    log(f"[target] {dev.FriendlyName}")

    iface = dev._dev.Activate(IAudioClient._iid_, CLSCTX_ALL, None)
    client = iface.QueryInterface(IAudioClient)
    fmt_ptr = client.GetMixFormat()
    fmt = fmt_ptr[0]
    # WAVEFORMATEX 字段
    log(f"[mix] channels={fmt.nChannels} rate={fmt.nSamplesPerSec} bits={fmt.wBitsPerSample} blockAlign={fmt.nBlockAlign} tag={fmt.wFormatTag}")
    try:
        hr = client.Initialize(0, 0x00000000 | 0x00080000, 2000000, 0, fmt_ptr, None)
        log(f"[init-shared] hr=0x{hr & 0xFFFFFFFF:08X}" + ("  OK" if hr == 0 else "  FAIL"))
        client.Stop()
    except OSError as e:
        log(f"[init-shared] EXCEPTION {e}")
    except Exception as e:
        log(f"[init-shared] EX {e!r}")


try:
    import threading

    def _wrap(fn):
        try:
            fn()
        except Exception as e:
            import traceback
            log("[thread-FAIL] " + repr(e))
            log(traceback.format_exc())

    th = threading.Thread(target=lambda: _wrap(main), daemon=True)
    th.start()
    th.join(20)
except Exception as e:
    import traceback
    log("[FAIL] " + repr(e))
    log(traceback.format_exc())
log("[end]")
print("done")
