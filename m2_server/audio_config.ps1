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
        int Activate(ref Guid iid, int dwClsCtx, IntPtr act, out object pp);
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
                    var v2 = k.GetValue("{a45c254e-df1c-4efd-8020-67d146a850e0},2");
                    if (v2 is string) return (string)v2;
                    var v14 = k.GetValue("{A45C254E-DF1C-4EFD-8020-67D146A850E0},14");
                    if (v14 is string) return (string)v14;
                    return null;
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
                pb = FindId(0, renderSub, captureSub);
            }
            string co = FindId(1, renderSub, captureSub);
            if (pb == null || co == null) return "{\"ok\":false,\"error\":\"device_not_found\",\"playback\":"+J(pb)+",\"capture\":"+J(co)+"}";
            bool saved = false;
            if (!System.IO.File.Exists(backupPath)) {
                var lines = new string[6];
                for (int r=0;r<3;r++) lines[r] = "R"+r+"="+GetDefaultId(0,r);
                for (int r=0;r<3;r++) lines[3+r] = "C"+r+"="+GetDefaultId(1,r);
                try { System.IO.File.WriteAllLines(backupPath, lines); saved = true; } catch {}
            }
            for (int role=0; role<3; role++){ SetDevice(pb, role); SetDevice(co, role); }
            return "{\"ok\":true,\"playback\":"+J(pb)+",\"capture\":"+J(co)+",\"backupSaved\":"+(saved?"true":"false")+"}";
        }
        public static string Restore(string backupPath) {
            if (!System.IO.File.Exists(backupPath)) return "{\"ok\":true,\"restored\":false,\"reason\":\"no_backup\"}";
            var dict = new Dictionary<string,string>();
            foreach (var line in System.IO.File.ReadAllLines(backupPath)) {
                int eq = line.IndexOf('=');
                if (eq > 0) dict[line.Substring(0,eq)] = line.Substring(eq+1);
            }
            for (int r=0;r<3;r++){ if (dict.ContainsKey("R"+r) && dict["R"+r]!="") SetDevice(dict["R"+r],r); }
            for (int r=0;r<3;r++){ if (dict.ContainsKey("C"+r) && dict["C"+r]!="") SetDevice(dict["C"+r],r); }
            try { System.IO.File.Delete(backupPath); } catch {}
            return "{\"ok\":true,\"restored\":true}";
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
    default   { "error_unknown_action" }
}
