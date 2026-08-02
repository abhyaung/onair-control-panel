using System.Text.Json.Nodes;
using OnAir.Adapters;

namespace OnAir;

/// <summary>
/// Action registry and the background poller.
///
/// Two rules carried over from the macOS agent, both learned by getting them
/// wrong:
///
/// * <b>A write is not confirmed by its return code.</b> Sending a command and
///   getting "success" says nothing about whether the value moved. Verify, and
///   report failure on this attempt if it did not — reporting success while
///   nothing changed is the worst outcome, because it hides a dead control.
/// * <b>Report state from the OS, not the page.</b> Mic state enumerates every
///   input device; camera state comes from the OS. Both stay correct with no
///   browser involved.
///
/// Browser/meeting control is deliberately absent. Windows has no AppleScript
/// equivalent, and driving Chrome via the DevTools protocol needs it relaunched
/// with a debug flag. The browser extension is the right answer and is shared
/// with the macOS agent — until it exists, this agent does hardware only.
/// </summary>
public sealed class Controls
{
    private readonly State _state;
    private CancellationTokenSource? _cts;

    public Controls(State state) => _state = state;

    public delegate JsonObject Action(Controls controls, JsonObject payload);

    public static readonly Dictionary<string, Action> Actions = new()
    {
        ["mic.toggle"] = (c, _) => c.ToggleMic(),
        ["volume.set"] = (c, p) => c.SetVolume(p),
        ["brightness.set"] = (c, p) => c.SetBrightness(p),
        ["mute.toggle"] = (c, _) => c.ToggleMute(),
        // camera.toggle intentionally missing until the extension exists;
        // an action that silently does nothing is worse than a 404.
    };

    // ── state building ───────────────────────────────────────────────────────

    public static JsonObject MicState()
    {
        var live = Audio.AnyLive();
        return new JsonObject
        {
            ["state"] = live is null ? "unknown" : (live.Value ? "live" : "off"),
            ["source"] = "devices",
            ["devices"] = new JsonArray(Audio.Status()
                .Select(d => (JsonNode?)new JsonObject
                {
                    ["name"] = d.Name,
                    ["muted"] = d.Muted,
                }).ToArray()),
        };
    }

    public static JsonObject CameraState()
    {
        var live = Camera.AnyLive();
        return new JsonObject
        {
            ["state"] = live is null ? "unknown" : (live.Value ? "live" : "off"),
            ["live_names"] = new JsonArray(
                Camera.LiveNames().Select(n => (JsonNode?)n).ToArray()),
        };
    }

    public static JsonObject VolumeState()
    {
        // The system mixer governs whatever device Windows is playing through.
        var level = Audio.OutputVolume();
        if (level.HasValue)
            return Level(level.Value, true, "system", null);

        // No software volume on the active device — try the display's own amp.
        var ddc = Display.Volume();
        if (ddc.HasValue)
            return Level(ddc.Value, true, "ddc", null);

        return Level(null, false, null, "no writable output");
    }

    public static JsonObject BrightnessState()
    {
        var level = Display.Brightness();
        return level.HasValue
            ? Level(level.Value, true, "ddc", null)
            : Level(null, false, null, "display does not report brightness over DDC");
    }

    public static JsonObject MuteState()
    {
        var muted = Audio.OutputMuted();
        if (muted.HasValue)
            return new JsonObject { ["muted"] = muted, ["writable"] = true, ["source"] = "system" };
        var ddc = Display.Muted();
        return new JsonObject
        {
            ["muted"] = ddc,
            ["writable"] = ddc.HasValue,
            ["source"] = ddc.HasValue ? "ddc" : null,
        };
    }

    private static JsonObject Level(float? value, bool writable, string? source, string? reason)
        => new()
        {
            ["value"] = value,
            ["writable"] = writable,
            ["source"] = source,
            ["reason"] = reason,
        };

    // ── actions ──────────────────────────────────────────────────────────────

    private JsonObject ToggleMic()
    {
        var live = Audio.AnyLive();
        if (live is null) return Fail("no readable input device");
        var results = Audio.MuteAll(live.Value);
        Publish(("mic", MicState()));
        return new JsonObject
        {
            ["ok"] = results.All(r => r.Ok),
            ["devices"] = new JsonArray(results
                .Select(r => (JsonNode?)new JsonObject { [r.Name] = r.Ok }).ToArray()),
        };
    }

    private JsonObject SetVolume(JsonObject payload)
    {
        if (payload["value"]?.GetValue<double>() is not double target)
            return Fail("value required");

        var before = VolumeState();
        var source = before["source"]?.GetValue<string>();
        var ok = source switch
        {
            "system" => Audio.SetOutputVolume((float)target),
            "ddc" => Display.SetVolume((float)target),
            _ => false,
        };
        if (!ok) return Fail("no writable output");

        // Verify: an API returning true is not proof the value moved.
        Thread.Sleep(150);
        var after = VolumeState();
        Publish(("volume", after), ("mute", MuteState()));
        var moved = after["value"]?.GetValue<double>();
        if (moved is null || Math.Abs(moved.Value - target) > 5)
            return Fail($"no change (reported {moved?.ToString() ?? "null"})");
        return Ok();
    }

    private JsonObject SetBrightness(JsonObject payload)
    {
        if (payload["value"]?.GetValue<double>() is not double target)
            return Fail("value required");
        if (!Display.SetBrightness((float)target))
            return Fail("display does not accept brightness over DDC");

        Thread.Sleep(150);
        var after = BrightnessState();
        Publish(("brightness", after));
        var moved = after["value"]?.GetValue<double>();
        if (moved is null || Math.Abs(moved.Value - target) > 5)
            return Fail($"no change (reported {moved?.ToString() ?? "null"})");
        return Ok();
    }

    private JsonObject ToggleMute()
    {
        var current = MuteState();
        if (current["writable"]?.GetValue<bool>() != true) return Fail("mute not available");
        var muted = current["muted"]?.GetValue<bool>() ?? false;
        var ok = current["source"]?.GetValue<string>() == "system"
            ? Audio.SetOutputMuted(!muted)
            : Display.SetMuted(!muted);
        Publish(("mute", MuteState()));
        return ok ? Ok() : Fail("mute failed");
    }

    private static JsonObject Ok() => new() { ["ok"] = true, ["reason"] = null };
    private static JsonObject Fail(string reason) => new() { ["ok"] = false, ["reason"] = reason };

    private void Publish(params (string Key, JsonNode Value)[] fields) =>
        _state.Update(fields.ToDictionary(f => f.Key, f => (JsonNode?)f.Value));

    // ── poller ───────────────────────────────────────────────────────────────

    public void StartPolling()
    {
        _cts = new CancellationTokenSource();
        var token = _cts.Token;
        _ = Task.Run(async () =>
        {
            while (!token.IsCancellationRequested)
            {
                try
                {
                    // Capture the sequence before reading. If an action lands
                    // while we read, our data is older than its and must be
                    // dropped rather than committed with a newer sequence.
                    var baseline = _state.Seq;
                    var fields = new Dictionary<string, JsonNode?>
                    {
                        ["mic"] = MicState(),
                        ["camera"] = CameraState(),
                        ["volume"] = VolumeState(),
                        ["brightness"] = BrightnessState(),
                        ["mute"] = MuteState(),
                        ["notes"] = Notes(),
                    };
                    _state.UpdateIf(baseline, fields);
                }
                catch (Exception)
                {
                    // A poller that dies takes the panel with it.
                }
                await Task.Delay(1000, token).ConfigureAwait(false);
            }
        }, token);
    }

    public void StopPolling() => _cts?.Cancel();

    private static JsonArray Notes()
    {
        var notes = new JsonArray();
        if (!Display.Available())
            notes.Add("display does not answer DDC — brightness and display volume unavailable");
        notes.Add("meeting controls need the browser extension (not built yet)");
        return notes;
    }
}
