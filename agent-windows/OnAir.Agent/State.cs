using System.Text.Json.Nodes;

namespace OnAir;

/// <summary>
/// Thread-safe snapshot of every control's state.
///
/// The <c>seq</c> / <c>boot</c> pair is not decoration — it is load-bearing, and
/// was arrived at by fixing three separate flicker bugs in the macOS agent:
///
/// 1. A poll already in flight when the user acts carries a snapshot from
///    *before* the change. Without ordering, it lands and undoes the optimistic
///    render, so the control visibly snaps back.
/// 2. The poller reads, then writes ~200ms later. If an action lands in that
///    gap, the poller's *older* data gets committed with a *newer* seq and
///    sails past the client's staleness gate. Hence <see cref="UpdateIf"/>:
///    the poller's write is dropped if anything else wrote during its read.
/// 3. <c>seq</c> restarts at 0 when the agent restarts, but the browser keeps
///    its high-water mark — so every subsequent poll is below the floor and is
///    discarded *forever*, freezing the panel while it still looks connected.
///    <c>boot</c> lets the client notice a restart and reset.
///
/// Do not simplify this away when porting.
/// </summary>
public sealed class State
{
    // Plain object: System.Threading.Lock is .NET 9+, this targets net8.0.
    private readonly object _gate = new();
    private readonly JsonObject _data;
    private long _seq;

    public State()
    {
        Boot = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds().ToString();
        _data = new JsonObject
        {
            ["seq"] = 0,
            ["boot"] = Boot,
            ["mic"] = Unknown(),
            ["camera"] = Unknown(),
            ["volume"] = Level(),
            ["mute"] = new JsonObject { ["muted"] = null, ["writable"] = false },
            ["brightness"] = Level(),
            ["context"] = new JsonObject { ["site"] = null, ["label"] = null },
            ["notes"] = new JsonArray(),
        };
    }

    public string Boot { get; }

    private static JsonObject Unknown() => new() { ["state"] = "unknown" };

    private static JsonObject Level() =>
        new() { ["value"] = null, ["writable"] = false, ["reason"] = null };

    public long Seq
    {
        get { lock (_gate) return _seq; }
    }

    public JsonObject Snapshot()
    {
        lock (_gate) return (JsonObject)_data.DeepClone();
    }

    public void Update(IDictionary<string, JsonNode?> fields)
    {
        lock (_gate) Write(fields);
    }

    /// <summary>
    /// Write only if nothing else has written since <paramref name="expectedSeq"/>.
    /// Returns false when a newer write intervened — the caller's data is stale
    /// and must be discarded rather than committed with a fresher sequence.
    /// </summary>
    public bool UpdateIf(long expectedSeq, IDictionary<string, JsonNode?> fields)
    {
        lock (_gate)
        {
            if (_seq != expectedSeq) return false;
            Write(fields);
            return true;
        }
    }

    private void Write(IDictionary<string, JsonNode?> fields)
    {
        _seq++;
        foreach (var (key, value) in fields)
            _data[key] = value?.DeepClone();
        _data["seq"] = _seq;
        _data["updated"] = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
    }
}
