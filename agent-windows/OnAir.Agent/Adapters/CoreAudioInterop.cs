using System.Runtime.InteropServices;

namespace OnAir.Adapters;

// COM declarations for WASAPI. Method order in each interface IS the vtable
// layout — adding, removing or reordering a member silently calls the wrong
// function rather than failing to compile. Keep these in documented order.
//
// UNVERIFIED: written without a Windows machine. If a call misbehaves, check
// the ordering here before suspecting the calling code.

[ComImport, Guid("BCDE0395-E52F-467C-8E3D-C4579291692E")]
internal class MMDeviceEnumerator { }

[ComImport, Guid("A95664D2-9614-4F35-A746-DE8DB63617E6"),
 InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
internal interface IMMDeviceEnumerator
{
    int EnumAudioEndpoints(int dataFlow, int stateMask, out IMMDeviceCollection devices);
    int GetDefaultAudioEndpoint(int dataFlow, int role, out IMMDevice endpoint);
    int GetDevice(string id, out IMMDevice device);
    int RegisterEndpointNotificationCallback(IntPtr client);
    int UnregisterEndpointNotificationCallback(IntPtr client);
}

[ComImport, Guid("0BD7A1BE-7A1A-44DB-8397-CC5392387B5E"),
 InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
internal interface IMMDeviceCollection
{
    int GetCount(out uint count);
    int Item(uint index, out IMMDevice device);
}

[ComImport, Guid("D666063F-1587-4E43-81F1-B948E807363F"),
 InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
internal interface IMMDevice
{
    int Activate(ref Guid iid, int clsCtx, IntPtr activationParams,
                 [MarshalAs(UnmanagedType.IUnknown)] out object instance);
    int OpenPropertyStore(int stgmAccess, out IPropertyStore properties);
    int GetId([MarshalAs(UnmanagedType.LPWStr)] out string id);
    int GetState(out int state);
}

[ComImport, Guid("886d8eeb-8cf2-4446-8d02-cdba1dbdcf99"),
 InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
internal interface IPropertyStore
{
    int GetCount(out int count);
    int GetAt(int index, out PropertyKey key);
    int GetValue(ref PropertyKey key, out PropVariant value);
    int SetValue(ref PropertyKey key, ref PropVariant value);
    int Commit();
}

[ComImport, Guid("5CDF2C82-841E-4546-9722-0CF74078229A"),
 InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
internal interface IAudioEndpointVolume
{
    int RegisterControlChangeNotify(IntPtr notify);
    int UnregisterControlChangeNotify(IntPtr notify);
    int GetChannelCount(out uint count);
    int SetMasterVolumeLevel(float levelDb, Guid eventContext);
    int SetMasterVolumeLevelScalar(float level, Guid eventContext);
    int GetMasterVolumeLevel(out float levelDb);
    int GetMasterVolumeLevelScalar(out float level);
    int SetChannelVolumeLevel(uint channel, float levelDb, Guid eventContext);
    int SetChannelVolumeLevelScalar(uint channel, float level, Guid eventContext);
    int GetChannelVolumeLevel(uint channel, out float levelDb);
    int GetChannelVolumeLevelScalar(uint channel, out float level);
    int SetMute([MarshalAs(UnmanagedType.Bool)] bool mute, Guid eventContext);
    int GetMute([MarshalAs(UnmanagedType.Bool)] out bool mute);
    int GetVolumeStepInfo(out uint step, out uint stepCount);
    int VolumeStepUp(Guid eventContext);
    int VolumeStepDown(Guid eventContext);
    int QueryHardwareSupport(out uint hardwareSupportMask);
    int GetVolumeRange(out float minDb, out float maxDb, out float incrementDb);
}

[StructLayout(LayoutKind.Sequential)]
internal struct PropertyKey
{
    public Guid FormatId;
    public int PropertyId;
}

internal static class PropertyKeys
{
    // PKEY_Device_FriendlyName
    public static PropertyKey DeviceFriendlyName = new()
    {
        FormatId = new Guid("a45c254e-df1c-4efd-8020-67d146a850e0"),
        PropertyId = 14,
    };
}

[StructLayout(LayoutKind.Explicit)]
internal struct PropVariant
{
    [FieldOffset(0)] public ushort VarType;
    [FieldOffset(8)] public IntPtr PointerValue;

    private const ushort VT_LPWSTR = 31;

    public string? AsString() =>
        VarType == VT_LPWSTR ? Marshal.PtrToStringUni(PointerValue) : null;
}
