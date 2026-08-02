using System.Runtime.InteropServices;

namespace OnAir.Adapters;

/// <summary>
/// Microphone control across <b>every</b> input device, via WASAPI.
///
/// Enumerating all devices rather than just the default one is the whole point.
/// On macOS the equivalent bug was live: the panel muted the default input while
/// the meeting was capturing from a different microphone, so it reported "off"
/// while the user was fully audible. A master switch that mutes one arbitrary
/// device is not a master switch.
///
/// UNVERIFIED — written without a Windows machine to run it on. The COM
/// interface layouts below are the documented vtable orders; if something
/// misbehaves, method ordering in the interface declarations is the first place
/// to look, because a wrong slot silently calls the wrong function.
/// </summary>
public static class Audio
{
    private const int DEVICE_STATE_ACTIVE = 0x1;
    private const int eCapture = 1;      // EDataFlow
    private const int eConsole = 0;      // ERole
    private const int STGM_READ = 0x0;

    public sealed record Device(string Id, string Name, bool? Muted);

    public static List<Device> Status()
    {
        var result = new List<Device>();
        IMMDeviceEnumerator? enumerator = null;
        try
        {
            enumerator = (IMMDeviceEnumerator)new MMDeviceEnumerator();
            enumerator.EnumAudioEndpoints(eCapture, DEVICE_STATE_ACTIVE, out var collection);
            collection.GetCount(out var count);
            for (uint i = 0; i < count; i++)
            {
                collection.Item(i, out var device);
                device.GetId(out var id);
                result.Add(new Device(id, FriendlyName(device), ReadMute(device)));
            }
        }
        catch (Exception)
        {
            // A device disappearing mid-enumeration must not take the poller
            // down; an empty list reads as "unknown", which is honest.
        }
        finally
        {
            if (enumerator is not null) Marshal.ReleaseComObject(enumerator);
        }
        return result;
    }

    /// <summary>
    /// True if any *readable* device is open, false if none, null if undeterminable.
    ///
    /// Devices whose state cannot be read are excluded rather than counted as
    /// live. Counting them sounds safer, but an inert virtual microphone then
    /// pins the panel to "live" permanently and the indicator stops meaning
    /// anything.
    /// </summary>
    public static bool? AnyLive()
    {
        var known = Status().Where(d => d.Muted.HasValue).ToList();
        if (known.Count == 0) return null;
        return known.Any(d => d.Muted == false);
    }

    public static IEnumerable<string> Unreadable() =>
        Status().Where(d => !d.Muted.HasValue).Select(d => d.Name);

    public static List<(string Name, bool Ok)> MuteAll(bool muted)
    {
        var results = new List<(string, bool)>();
        IMMDeviceEnumerator? enumerator = null;
        try
        {
            enumerator = (IMMDeviceEnumerator)new MMDeviceEnumerator();
            enumerator.EnumAudioEndpoints(eCapture, DEVICE_STATE_ACTIVE, out var collection);
            collection.GetCount(out var count);
            for (uint i = 0; i < count; i++)
            {
                collection.Item(i, out var device);
                results.Add((FriendlyName(device), WriteMute(device, muted)));
            }
        }
        catch (Exception ex)
        {
            results.Add((ex.GetType().Name, false));
        }
        finally
        {
            if (enumerator is not null) Marshal.ReleaseComObject(enumerator);
        }
        return results;
    }

    // ── output side ──────────────────────────────────────────────────────────

    public static float? OutputVolume() => WithDefaultRender(v =>
    {
        v.GetMasterVolumeLevelScalar(out var level);
        return level * 100f;
    });

    public static bool SetOutputVolume(float percent) => WithDefaultRender(v =>
    {
        v.SetMasterVolumeLevelScalar(Math.Clamp(percent, 0f, 100f) / 100f, Guid.Empty);
        return 1f;
    }).HasValue;

    public static bool? OutputMuted() => WithDefaultRender(v =>
    {
        v.GetMute(out var muted);
        return muted ? 1f : 0f;
    }) switch { null => null, 0f => false, _ => true };

    public static bool SetOutputMuted(bool muted) => WithDefaultRender(v =>
    {
        v.SetMute(muted, Guid.Empty);
        return 1f;
    }).HasValue;

    private static float? WithDefaultRender(Func<IAudioEndpointVolume, float> action)
    {
        IMMDeviceEnumerator? enumerator = null;
        try
        {
            enumerator = (IMMDeviceEnumerator)new MMDeviceEnumerator();
            enumerator.GetDefaultAudioEndpoint(0 /* eRender */, eConsole, out var device);
            var iid = typeof(IAudioEndpointVolume).GUID;
            device.Activate(ref iid, 1 /* CLSCTX_INPROC_SERVER */, IntPtr.Zero, out var o);
            return action((IAudioEndpointVolume)o);
        }
        catch (Exception) { return null; }
        finally { if (enumerator is not null) Marshal.ReleaseComObject(enumerator); }
    }

    // ── helpers ──────────────────────────────────────────────────────────────

    private static bool? ReadMute(IMMDevice device)
    {
        try
        {
            var iid = typeof(IAudioEndpointVolume).GUID;
            device.Activate(ref iid, 1, IntPtr.Zero, out var o);
            ((IAudioEndpointVolume)o).GetMute(out var muted);
            return muted;
        }
        catch (Exception) { return null; }
    }

    private static bool WriteMute(IMMDevice device, bool muted)
    {
        try
        {
            var iid = typeof(IAudioEndpointVolume).GUID;
            device.Activate(ref iid, 1, IntPtr.Zero, out var o);
            ((IAudioEndpointVolume)o).SetMute(muted, Guid.Empty);
            return true;
        }
        catch (Exception) { return false; }
    }

    private static string FriendlyName(IMMDevice device)
    {
        try
        {
            device.OpenPropertyStore(STGM_READ, out var store);
            var key = PropertyKeys.DeviceFriendlyName;
            store.GetValue(ref key, out var variant);
            return variant.AsString() ?? "unknown device";
        }
        catch (Exception) { return "unknown device"; }
    }
}
