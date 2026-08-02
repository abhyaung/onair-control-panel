using System.Runtime.InteropServices;

namespace OnAir.Adapters;

/// <summary>
/// External-display brightness and speaker volume over DDC/CI.
///
/// This is the one area where Windows is markedly better than macOS. On macOS
/// there is no API at all: the agent there depends on MonitorControl being
/// installed and drives it by synthesising media keys, which in turn needs an
/// Accessibility grant and a code-signed app. Windows exposes DDC directly
/// through Dxva2 — brightness has a dedicated call, and everything else
/// (including audio volume, VCP 0x62) goes through SetVCPFeature.
///
/// No permissions required.
///
/// UNVERIFIED — written without a Windows machine.
/// </summary>
public static class Display
{
    private const byte VCP_AUDIO_VOLUME = 0x62;
    private const byte VCP_AUDIO_MUTE = 0x8D;

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct PHYSICAL_MONITOR
    {
        public IntPtr hPhysicalMonitor;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 128)]
        public string szPhysicalMonitorDescription;
    }

    [DllImport("user32.dll")]
    private static extern IntPtr MonitorFromWindow(IntPtr hwnd, uint flags);

    [DllImport("dxva2.dll", SetLastError = true)]
    private static extern bool GetNumberOfPhysicalMonitorsFromHMONITOR(
        IntPtr hMonitor, out uint count);

    [DllImport("dxva2.dll", SetLastError = true)]
    private static extern bool GetPhysicalMonitorsFromHMONITOR(
        IntPtr hMonitor, uint count, [Out] PHYSICAL_MONITOR[] monitors);

    [DllImport("dxva2.dll", SetLastError = true)]
    private static extern bool DestroyPhysicalMonitors(uint count, PHYSICAL_MONITOR[] monitors);

    [DllImport("dxva2.dll", SetLastError = true)]
    private static extern bool GetMonitorBrightness(
        IntPtr hMonitor, out uint min, out uint current, out uint max);

    [DllImport("dxva2.dll", SetLastError = true)]
    private static extern bool SetMonitorBrightness(IntPtr hMonitor, uint brightness);

    [DllImport("dxva2.dll", SetLastError = true)]
    private static extern bool GetVCPFeatureAndVCPFeatureReply(
        IntPtr hMonitor, byte code, IntPtr type, out uint current, out uint max);

    [DllImport("dxva2.dll", SetLastError = true)]
    private static extern bool SetVCPFeature(IntPtr hMonitor, byte code, uint value);

    private const uint MONITOR_DEFAULTTOPRIMARY = 1;

    /// <summary>Run an action against the primary monitor's DDC handle.</summary>
    private static T? WithMonitor<T>(Func<IntPtr, T?> action) where T : struct
    {
        var hMonitor = MonitorFromWindow(IntPtr.Zero, MONITOR_DEFAULTTOPRIMARY);
        if (hMonitor == IntPtr.Zero) return null;
        if (!GetNumberOfPhysicalMonitorsFromHMONITOR(hMonitor, out var count) || count == 0)
            return null;

        var monitors = new PHYSICAL_MONITOR[count];
        if (!GetPhysicalMonitorsFromHMONITOR(hMonitor, count, monitors)) return null;
        try
        {
            return action(monitors[0].hPhysicalMonitor);
        }
        catch (Exception) { return null; }
        finally { DestroyPhysicalMonitors(count, monitors); }
    }

    public static float? Brightness() => WithMonitor<float>(h =>
        GetMonitorBrightness(h, out var min, out var cur, out var max) && max > min
            ? (float?)((cur - min) * 100f / (max - min))
            : null);

    public static bool SetBrightness(float percent) => WithMonitor<bool>(h =>
    {
        if (!GetMonitorBrightness(h, out var min, out _, out var max)) return false;
        var target = min + (uint)Math.Round(Math.Clamp(percent, 0, 100) / 100f * (max - min));
        return SetMonitorBrightness(h, target);
    }) ?? false;

    public static float? Volume() => WithMonitor<float>(h =>
        GetVCPFeatureAndVCPFeatureReply(h, VCP_AUDIO_VOLUME, IntPtr.Zero, out var cur, out var max)
        && max > 0 ? (float?)(cur * 100f / max) : null);

    public static bool SetVolume(float percent) => WithMonitor<bool>(h =>
    {
        if (!GetVCPFeatureAndVCPFeatureReply(h, VCP_AUDIO_VOLUME, IntPtr.Zero, out _, out var max)
            || max == 0) return false;
        return SetVCPFeature(h, VCP_AUDIO_VOLUME,
                             (uint)Math.Round(Math.Clamp(percent, 0, 100) / 100f * max));
    }) ?? false;

    /// <summary>DDC audio mute is an enum: 1 = muted, 2 = unmuted. Not a level.</summary>
    public static bool? Muted() => WithMonitor<bool>(h =>
        GetVCPFeatureAndVCPFeatureReply(h, VCP_AUDIO_MUTE, IntPtr.Zero, out var cur, out _)
            ? (bool?)(cur == 1)
            : null);

    public static bool SetMuted(bool muted) =>
        WithMonitor<bool>(h => SetVCPFeature(h, VCP_AUDIO_MUTE, muted ? 1u : 2u)) ?? false;

    /// <summary>
    /// Whether this display answers DDC at all.
    ///
    /// Worth probing rather than assuming: on macOS the Thunderbolt display
    /// accepted DDC reads and returned pure noise (0, 3, 0, 0, 0 while sitting
    /// steady at 51%), which a naive implementation trusted. Consistency alone
    /// is not proof either — three identical zeros passed that test.
    /// </summary>
    public static bool Available() => Brightness().HasValue || Volume().HasValue;
}
