# onair — Windows agent

Not built yet. Implements the same contract as the macOS agent
(see `../protocol/API.md`), so the panel in `../panel/` works unchanged.

Most of it is *easier* than on macOS:

| Capability | Windows API |
|---|---|
| Mute every input device | `IMMDeviceEnumerator` → `IAudioEndpointVolume::SetMute` |
| Camera in use | `CapabilityAccessManager\ConsentStore\webcam` registry keys |
| Monitor brightness/volume | `SetMonitorBrightness` / `SetVCPFeature` — native DDC/CI |
| System volume | `IAudioEndpointVolume::SetMasterVolumeLevelScalar` |
| Input-synthesis permission | none required |

The hard part is the same on both platforms: driving the meeting page. Windows
has no AppleScript equivalent, so this needs a browser extension rather than a
per-OS hack — which is why the extension is worth doing before this agent.

Suggested language: **C#**. Single self-contained exe, no runtime to bundle,
first-class access to the audio and DDC APIs, and a tray icon is trivial.
