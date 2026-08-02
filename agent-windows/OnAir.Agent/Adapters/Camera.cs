using Microsoft.Win32;

namespace OnAir.Adapters;

/// <summary>
/// Whether any camera is currently capturing.
///
/// Windows has no direct "is the camera in use" API, but it records exactly
/// that for the privacy indicator: under CapabilityAccessManager, each app that
/// has used the webcam gets a <c>LastUsedTimeStop</c> value, and while the
/// camera is *open* that value is 0. Non-packaged (classic desktop) apps live
/// under a separate subkey, so both trees must be walked — Chrome is there.
///
/// This is the equivalent of the macOS agent reading CoreMediaIO rather than
/// the meeting page: it stays correct with no browser involved, in apps we know
/// nothing about, and with no meeting running.
///
/// UNVERIFIED — written without a Windows machine.
/// </summary>
public static class Camera
{
    private const string ConsentRoot =
        @"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\webcam";

    public sealed record Usage(string App, bool InUse);

    public static List<Usage> Devices()
    {
        var found = new List<Usage>();
        foreach (var root in new[] { Registry.CurrentUser, Registry.LocalMachine })
        {
            try
            {
                using var webcam = root.OpenSubKey(ConsentRoot);
                if (webcam is null) continue;
                Walk(webcam, found);
                using var nonPackaged = webcam.OpenSubKey("NonPackaged");
                if (nonPackaged is not null) Walk(nonPackaged, found);
            }
            catch (Exception)
            {
                // Registry access can fail per-hive; a partial answer beats none.
            }
        }
        return found;
    }

    private static void Walk(RegistryKey parent, List<Usage> into)
    {
        foreach (var name in parent.GetSubKeyNames())
        {
            if (name.Equals("NonPackaged", StringComparison.OrdinalIgnoreCase)) continue;
            try
            {
                using var app = parent.OpenSubKey(name);
                if (app?.GetValue("LastUsedTimeStop") is not long stop) continue;
                // 0 means "still open". Anything else is a timestamp of when it
                // stopped, which means it is not in use now.
                into.Add(new Usage(Pretty(name), stop == 0));
            }
            catch (Exception) { }
        }
    }

    /// <summary>True if any camera is capturing, false if none, null if unreadable.</summary>
    public static bool? AnyLive()
    {
        var devices = Devices();
        if (devices.Count == 0) return null;
        return devices.Any(d => d.InUse);
    }

    public static IEnumerable<string> LiveNames() =>
        Devices().Where(d => d.InUse).Select(d => d.App);

    // Registry keys encode paths with '#' separators; show the executable name.
    private static string Pretty(string key)
    {
        var parts = key.Split('#', StringSplitOptions.RemoveEmptyEntries);
        return parts.Length > 0 ? parts[^1] : key;
    }
}
