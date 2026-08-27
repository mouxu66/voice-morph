param(
    [string]$action = "status",
    [string]$renderSub = "",
    [string]$captureSub = "CABLE Output"
)

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$backupPath = Join-Path $env:LOCALAPPDATA "rvc_audio_backup.txt"

$code = @'
using System;
using System.Runtime.InteropServices;
using Microsoft.Win32;
using System.Text;
using System.Collections.Generic;

namespace CoreAudio {
    [Guid("A95664D2-9614-4F35-A746-DE8DB63617E6")]
    [InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    public interface IMMDeviceEnumerator {
        int EnumAudioEndpoints(int dataFlow, int stateMask, out IMMDeviceCollection ppDevices);
        int GetDefaultAudioEndpoint(int dataFlow, int role, out IMMDevice ppEndpoint);
        int GetDevice([MarshalAs(UnmanagedType.LPWStr)] string id, out IMMDevice ppDevice);
        int RegisterEndpointNotificationCallback(IntPtr p);
        int UnregisterEndpointNotificationCallback(IntPtr p);
    }
    [Guid("0BD7A1BE-7A1A-44DB-8397-CC5392387B5E")]
    [InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    public interface IMMDeviceCollection {
        int GetCount(out int pnCount);
        int Item(int n, out IMMDevice ppDevice);
    }
    [Guid("D666063F-1587-4E43-81F1-B948E807363F")]
    [InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    public interface IMMDevice {
        int Activate(ref Guid iid, int dwClsCtx, IntPtr act, out IntPtr pp);
        int OpenPropertyStore(int stgmAccess, out IntPtr pp);
        int GetId(out IntPtr ppstrId);
        int GetState(out int st);
    }
    [Guid("F8679F50-850A-41CF-9C72-430F290290C8")]
    [InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    public interface IPolicyConfig {
        int GetMixFormat(IntPtr a, IntPtr b);
        int GetDeviceFormat(IntPtr a, IntPtr b);
        int ResetDeviceFormat(IntPtr a);
        int SetDeviceFormat(IntPtr a, IntPtr b);
        int GetProcessingPeriod(IntPtr a, IntPtr b, IntPtr c);
        int SetProcessingPeriod(IntPtr a, IntPtr b);
        int GetShareMode(IntPtr a, IntPtr b);
        int SetShareMode(IntPtr a, IntPtr b);
        int GetPropertyValue(IntPtr a, IntPtr b, IntPtr c);
        int SetPropertyValue(IntPtr a, IntPtr b, IntPtr c);
        [PreserveSig] int SetDefaultEndpoint([MarshalAs(UnmanagedType.LPWStr)] string wszDeviceId, int eRole);
    }
    [ComImport, Guid("BCDE0395-E52F-467C-8E3D-C4579291692E")]
    public class MMDeviceEnumerator { }
    [ComImport, Guid("870AF99C-171D-4F9E-AF0D-E63DF40C2BC9")]
    public class PolicyConfigClient { }

    public class Audio {
        static string J(string s) {
            if (s == null) return "null";
            return "\"" + s.Replace("\\","\\\\").Replace("\"","\\\"") + "\"";
        }
        static string GetName(string id, bool render) {
            int i = id.IndexOf('{', 1);
            if (i < 0) return null;
            string guid = id.Substring(i);
            string sk = render ? "Render" : "Capture";
            string path = @"SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\" + sk + @"\" + guid + @"\Properties";
            using (var baseKey = RegistryKey.OpenBaseKey(RegistryHive.LocalMachine, RegistryView.Registry64)) {
                using (var k = baseKey.OpenSubKey(path)) {
                    if (k == null) return null;
                    // pid2 = 设备描述（"扬声器"），{b3f8fa53},6 = 设备提供方（"Senary Audio"/"Steam Streaming Speakers"）
                    // 组合成声音面板显示的完整名 "扬声器 (Senary Audio)"，才能区分 Steam 虚拟设备
                    var v2 = k.GetValue("{a45c254e-df1c-4efd-8020-67d146a850e0},2") as string;
                    if (v2 == null) v2 = k.GetValue("{A45C254E-DF1C-4EFD-8020-67D146A850E0},14") as string;
                    if (v2 == null) return null;
                    var prov = k.GetValue("{b3f8fa53-0004-438e-9003-51a46e139bfc},6") as string;
                    if (!string.IsNullOrWhiteSpace(prov) && !v2.Contains(prov)) return v2 + " (" + prov.TrimEnd() + ")";
                    return v2;
                }
            }
        }
        public static string FindId(int flow, string renderSub, string captureSub) {
            var e = (IMMDeviceEnumerator)new MMDeviceEnumerator();
            IMMDeviceCollection coll; e.EnumAudioEndpoints(flow, 1, out coll);
            int cnt; coll.GetCount(out cnt);
            string want = (flow == 0) ? renderSub : captureSub;
            for (int i = 0; i < cnt; i++) {
                IMMDevice d; coll.Item(i, out d);
                IntPtr p; d.GetId(out p); string id = Marshal.PtrToStringUni(p); Marshal.FreeCoTaskMem(p);
                string nm = GetName(id, flow == 0);
                if (nm != null && nm.IndexOf(want, StringComparison.Ordinal) >= 0) {
                    if (flow == 0 && nm.IndexOf("CABLE", StringComparison.OrdinalIgnoreCase) >= 0) continue;
                    return id;
                }
            }
            return null;
        }
        public static void SetDevice(string id, int role) {
            var obj = (IPolicyConfig)new PolicyConfigClient();
            int hr = obj.SetDefaultEndpoint(id, role);
            if (hr < 0) throw new Exception("SetDefaultEndpoint failed hr=" + hr);
        }
        // 设置设备并立即验证是否真实生效；返回 null=成功，否则返回错误描述
        public static string SetDeviceVerified(string id, int role) {
            try {
                var obj = (IPolicyConfig)new PolicyConfigClient();
                int hr = obj.SetDefaultEndpoint(id, role);
                if (hr < 0) return "SetDefaultEndpoint hr=" + hr;
            } catch (Exception ex) { return ex.Message; }
            try {
                System.Threading.Thread.Sleep(120);
                int flow = id.IndexOf("0.0.1", StringComparison.Ordinal) >= 0 ? 1 : 0;
                var e = (IMMDeviceEnumerator)new MMDeviceEnumerator();
                IMMDevice d; int gh = e.GetDefaultAudioEndpoint(flow, role, out d);
                if (gh != 0) return "verify_get_default_failed_hr=" + gh;
                IntPtr p; d.GetId(out p); string cur = Marshal.PtrToStringUni(p); Marshal.FreeCoTaskMem(p);
                if (cur != id) return "verify_mismatch want=" + id + " got=" + cur;
                return null;
            } catch (Exception ex) { return "verify_ex:" + ex.Message; }
        }
        // 枚举当前激活设备 ID 列表
        static System.Collections.Generic.List<string> EnumerateIds(int flow) {
            var e = (IMMDeviceEnumerator)new MMDeviceEnumerator();
            IMMDeviceCollection coll; e.EnumAudioEndpoints(flow, 1, out coll);
            int cnt; coll.GetCount(out cnt);
            var list = new System.Collections.Generic.List<string>();
            for (int i = 0; i < cnt; i++) {
                IMMDevice d; coll.Item(i, out d);
                IntPtr p; d.GetId(out p); string id = Marshal.PtrToStringUni(p); Marshal.FreeCoTaskMem(p);
                list.Add(id);
            }
            return list;
        }
        // 校验备份文件中的设备 ID 当前是否仍然存在，且不是 CABLE / Steam 虚拟设备（存在才设置，避免坏 ID 中断，
        // 也避免把默认设备设回 CABLE 或 Steam 串流设备导致"恢复无效"）
        static void ValidateBackupDict(System.Collections.Generic.Dictionary<string,string> dict, out string[] renderIds, out string[] captureIds) {
            var rlist = EnumerateIds(0);
            var clist = EnumerateIds(1);
            renderIds = new string[3]; captureIds = new string[3];
            for (int r=0;r<3;r++) {
                string id = dict.ContainsKey("R"+r) ? dict["R"+r] : "";
                if (id == "" || !rlist.Contains(id)) { renderIds[r] = null; continue; }
                string n = GetName(id, true);
                if (n != null && n.IndexOf("CABLE", StringComparison.OrdinalIgnoreCase) >= 0) { renderIds[r] = null; continue; }
                if (n != null && n.IndexOf("Steam Streaming", StringComparison.OrdinalIgnoreCase) >= 0) { renderIds[r] = null; continue; }
                renderIds[r] = id;
            }
            for (int r=0;r<3;r++) {
                string id = dict.ContainsKey("C"+r) ? dict["C"+r] : "";
                if (id == "" || !clist.Contains(id)) { captureIds[r] = null; continue; }
                string n = GetName(id, false);
                if (n != null && n.IndexOf("CABLE", StringComparison.OrdinalIgnoreCase) >= 0) { captureIds[r] = null; continue; }
                if (n != null && n.IndexOf("Steam Streaming", StringComparison.OrdinalIgnoreCase) >= 0) { captureIds[r] = null; continue; }
                captureIds[r] = id;
            }
        }
        public static string GetDefaultId(int flow, int role) {
            var e = (IMMDeviceEnumerator)new MMDeviceEnumerator();
            IMMDevice d; e.GetDefaultAudioEndpoint(flow, role, out d);
            IntPtr p; d.GetId(out p); string id = Marshal.PtrToStringUni(p); Marshal.FreeCoTaskMem(p); return id;
        }
        public static string ListDevices() {
            var e = (IMMDeviceEnumerator)new MMDeviceEnumerator();
            StringBuilder sb = new StringBuilder();
            sb.Append("{\"ok\":true,\"devices\":[");
            bool first = true;
            for (int flow = 0; flow < 2; flow++) {
                IMMDeviceCollection coll; e.EnumAudioEndpoints(flow, 1, out coll);
                int cnt; coll.GetCount(out cnt);
                for (int i = 0; i < cnt; i++) {
                    IMMDevice d; coll.Item(i, out d);
                    IntPtr p; d.GetId(out p); string id = Marshal.PtrToStringUni(p); Marshal.FreeCoTaskMem(p);
                    string nm = GetName(id, flow == 0);
                    if (!first) sb.Append(","); first = false;
                    sb.Append("{\"flow\":" + flow + ",\"name\":" + J(nm) + ",\"id\":" + J(id) + "}");
                }
            }
            sb.Append("]}");
            return sb.ToString();
        }
        public static string CurrentStatus() {
            return "{\"ok\":true," +
                "\"render\":{\"0\":" + J(GetName(GetDefaultId(0,0),true)) + ",\"1\":" + J(GetName(GetDefaultId(0,1),true)) + ",\"2\":" + J(GetName(GetDefaultId(0,2),true)) + "}," +
                "\"capture\":{\"0\":" + J(GetName(GetDefaultId(1,0),false)) + ",\"1\":" + J(GetName(GetDefaultId(1,1),false)) + ",\"2\":" + J(GetName(GetDefaultId(1,2),false)) + "}}";
        }
        public static string ApplyOptimal(string renderSub, string captureSub, string backupPath) {
            string pb = GetDefaultId(0, 1);
            string pbName = GetName(pb, true);
            if (pbName == null || pbName.IndexOf("CABLE", StringComparison.OrdinalIgnoreCase) >= 0) {
                pb = FindReal(0);
            }
            string co = FindId(1, renderSub, captureSub);
            if (pb == null || co == null) return "{\"ok\":false,\"error\":\"device_not_found\",\"playback\":"+J(pb)+",\"capture\":"+J(co)+"}";
            bool saved = false;
            // 备份时确保保存的是"真实设备"：当前默认若是 CABLE 虚拟设备，则改用真实设备，
            // 否则 restore 会把默认设备设回 CABLE，导致"恢复无效"。
            if (!System.IO.File.Exists(backupPath)) {
                var lines = new string[6];
                for (int r=0;r<3;r++) {
                    string d = GetDefaultId(0,r);
                    string n = GetName(d, true);
                    // CABLE 和 Steam 虚拟设备都不能作为"用户原始设备"写入备份
                    if (n != null && n.IndexOf("CABLE", StringComparison.OrdinalIgnoreCase) >= 0) { string rd = FindReal(0); if (rd != null) d = rd; }
                    if (n != null && n.IndexOf("Steam Streaming", StringComparison.OrdinalIgnoreCase) >= 0) { string rd = FindReal(0); if (rd != null) d = rd; }
                    lines[r] = "R"+r+"="+d;
                }
                for (int r=0;r<3;r++) {
                    string d = GetDefaultId(1,r);
                    string n = GetName(d, false);
                    if (n != null && n.IndexOf("CABLE", StringComparison.OrdinalIgnoreCase) >= 0) { string rd = FindReal(1); if (rd != null) d = rd; }
                    if (n != null && n.IndexOf("Steam Streaming", StringComparison.OrdinalIgnoreCase) >= 0) { string rd = FindReal(1); if (rd != null) d = rd; }
                    lines[3+r] = "C"+r+"="+d;
                }
                try { System.IO.File.WriteAllLines(backupPath, lines); saved = true; } catch {}
            }
            var errors = new System.Collections.Generic.List<string>();
            int okCount = 0;
            for (int role=0; role<3; role++){
                string er = SetDeviceVerified(pb, role); if (er == null) okCount++; else errors.Add("R"+role+":"+er);
                string ec = SetDeviceVerified(co, role); if (ec == null) okCount++; else errors.Add("C"+role+":"+ec);
            }
            if (errors.Count > 0)
                return "{\"ok\":false,\"error\":\"apply_partial\",\"setted\":"+okCount+",\"playback\":"+J(pb)+",\"capture\":"+J(co)+",\"backupSaved\":"+(saved?"true":"false")+",\"errors\":[" + string.Join(",", errors.ConvertAll(J).ToArray()) + "]}";
            return "{\"ok\":true,\"playback\":"+J(pb)+",\"capture\":"+J(co)+",\"backupSaved\":"+(saved?"true":"false")+"}";
        }
        // 找到"真实"设备（当前激活、非 CABLE、非 Steam 虚拟设备；优先 Senary 本机声卡）
        public static string FindReal(int flow) {
            var e = (IMMDeviceEnumerator)new MMDeviceEnumerator();
            IMMDeviceCollection coll; e.EnumAudioEndpoints(flow, 1, out coll);
            int cnt; coll.GetCount(out cnt);
            string fallback = null;
            for (int i = 0; i < cnt; i++) {
                IMMDevice d; coll.Item(i, out d);
                IntPtr p; d.GetId(out p); string id = Marshal.PtrToStringUni(p); Marshal.FreeCoTaskMem(p);
                string nm = GetName(id, flow == 0);
                if (nm == null) continue;
                if (nm.IndexOf("CABLE", StringComparison.OrdinalIgnoreCase) >= 0) continue;
                // Steam Streaming Speakers/Microphone 是 Steam 串流虚拟设备，绝不能当真实设备
                if (nm.IndexOf("Steam Streaming", StringComparison.OrdinalIgnoreCase) >= 0) continue;
                // 本机真实声卡优先（Senary Audio），直接返回
                if (nm.IndexOf("Senary", StringComparison.OrdinalIgnoreCase) >= 0) return id;
                if (fallback == null) fallback = id;
            }
            return fallback;
        }
        public static string Reset(string backupPath) {
            var errors = new System.Collections.Generic.List<string>();
            // 优先用备份中记录的原始设备（用户真正在用的设备），备份无效则 FindReal
            string[] rTargets = null, cTargets = null;
            if (System.IO.File.Exists(backupPath)) {
                try {
                    var dict = new Dictionary<string,string>();
                    foreach (var line in System.IO.File.ReadAllLines(backupPath)) {
                        int eq = line.IndexOf('=');
                        if (eq > 0) dict[line.Substring(0,eq)] = line.Substring(eq+1);
                    }
                    string[] rl, cl;
                    ValidateBackupDict(dict, out rl, out cl);
                    bool rOk = false, cOk = false;
                    foreach (var x in rl) if (x != null) { rOk = true; break; }
                    foreach (var x in cl) if (x != null) { cOk = true; break; }
                    if (rOk) rTargets = rl;
                    if (cOk) cTargets = cl;
                } catch (Exception ex) { errors.Add("backup_parse:" + ex.Message); }
            }
            if (rTargets == null) { string pb = FindReal(0); if (pb == null) pb = GetDefaultId(0, 1); rTargets = new string[]{pb,pb,pb}; }
            if (cTargets == null) { string co = FindReal(1); if (co == null) co = GetDefaultId(1, 1); cTargets = new string[]{co,co,co}; }
            int okCount = 0;
            for (int role = 0; role < 3; role++) {
                if (rTargets[role] != null) { string er = SetDeviceVerified(rTargets[role], role); if (er == null) okCount++; else errors.Add("R"+role+":"+er); }
                if (cTargets[role] != null) { string ec = SetDeviceVerified(cTargets[role], role); if (ec == null) okCount++; else errors.Add("C"+role+":"+ec); }
            }
            bool allOk = (errors.Count == 0);
            if (allOk) { try { if (System.IO.File.Exists(backupPath)) System.IO.File.Delete(backupPath); } catch {} }
            string detail = allOk ? "" : ",\"errors\":[" + string.Join(",", errors.ConvertAll(J).ToArray()) + "]";
            return "{\"ok\":" + (allOk?"true":"false") + ",\"reset\":true,\"setted\":" + okCount + ",\"playback\":" + J(rTargets[0]) + ",\"capture\":" + J(cTargets[0]) + detail + "}";
        }
        public static string Restore(string backupPath) {
            if (!System.IO.File.Exists(backupPath)) return Reset(backupPath);
            var dict = new Dictionary<string,string>();
            try {
                foreach (var line in System.IO.File.ReadAllLines(backupPath)) {
                    int eq = line.IndexOf('=');
                    if (eq > 0) dict[line.Substring(0,eq)] = line.Substring(eq+1);
                }
            } catch (Exception ex) { return "{\"ok\":false,\"restored\":false,\"error\":" + J("backup_read:" + ex.Message) + "}"; }
            string[] rTargets, cTargets;
            ValidateBackupDict(dict, out rTargets, out cTargets);
            var errors = new System.Collections.Generic.List<string>();
            int okCount = 0;
            for (int r=0;r<3;r++){ if (rTargets[r] != null) { string er = SetDeviceVerified(rTargets[r], r); if (er == null) okCount++; else errors.Add("R"+r+":"+er); } else if (dict.ContainsKey("R"+r) && dict["R"+r]!="") errors.Add("R"+r+":device_missing"); }
            for (int r=0;r<3;r++){ if (cTargets[r] != null) { string ec = SetDeviceVerified(cTargets[r], r); if (ec == null) okCount++; else errors.Add("C"+r+":"+ec); } else if (dict.ContainsKey("C"+r) && dict["C"+r]!="") errors.Add("C"+r+":device_missing"); }
            bool allOk = (errors.Count == 0);
            // 全部成功才删除备份；有失败保留备份供下次重试
            if (allOk) { try { System.IO.File.Delete(backupPath); } catch {} }
            string detail = allOk ? "" : ",\"errors\":[" + string.Join(",", errors.ConvertAll(J).ToArray()) + "]";
            return "{\"ok\":" + (allOk?"true":"false") + ",\"restored\":" + (allOk?"true":"false") + ",\"setted\":" + okCount + detail + "}";
        }
        public static string Diagnostic() {
            var e = (IMMDeviceEnumerator)new MMDeviceEnumerator();
            var sb = new StringBuilder();
            sb.Append("{\"ok\":true,\"devices\":[");
            bool first = true;
            for (int flow = 0; flow < 2; flow++) {
                IMMDeviceCollection coll; e.EnumAudioEndpoints(flow, 7, out coll);
                int cnt; coll.GetCount(out cnt);
                for (int i = 0; i < cnt; i++) {
                    IMMDevice d; coll.Item(i, out d);
                    IntPtr p; d.GetId(out p); string id = Marshal.PtrToStringUni(p); Marshal.FreeCoTaskMem(p);
                    int st; d.GetState(out st);
                    string nm = GetName(id, flow == 0);
                    var roles = new System.Collections.Generic.List<int>();
                    for (int r = 0; r < 3; r++) { string def = GetDefaultId(flow, r); if (def == id) roles.Add(r); }
                    if (!first) sb.Append(",");
                    first = false;
                    sb.Append("{\"flow\":" + flow + ",\"name\":" + J(nm) + ",\"state\":" + st + ",\"roles\":[" + string.Join(",", roles.ToArray()) + "]}");
                }
            }
            sb.Append("]}");
            return sb.ToString();
        }
    }
}
'@
Add-Type -TypeDefinition $code

switch ($action) {
    "list"    { [CoreAudio.Audio]::ListDevices() }
    "status"  { [CoreAudio.Audio]::CurrentStatus() }
    "apply"   { [CoreAudio.Audio]::ApplyOptimal($renderSub, $captureSub, $backupPath) }
    "restore" { [CoreAudio.Audio]::Restore($backupPath) }
    "reset"   { [CoreAudio.Audio]::Reset($backupPath) }
    "diag"    { [CoreAudio.Audio]::Diagnostic() }
    default   { "error_unknown_action" }
}
