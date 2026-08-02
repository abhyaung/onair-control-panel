using System.Net;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace OnAir;

/// <summary>
/// HTTP server for the panel. Mirrors the macOS agent's contract exactly —
/// see protocol/API.md.
///
/// Security model, in order of importance:
///   1. The client sends action ids, never commands. Every id must already
///      exist in <see cref="Controls.Actions"/>. There is no path from client
///      input to a shell. This is load-bearing; the token is defence in depth.
///   2. Bearer token, generated on first run, compared in constant time.
///   3. Bound to the LAN address, never 0.0.0.0.
///
/// UNVERIFIED — written without a Windows machine.
/// </summary>
public sealed class Server
{
    private readonly HttpListener _listener = new();
    private readonly State _state;
    private readonly Controls _controls;
    private readonly string _webRoot;

    public string Token { get; }
    public string Url { get; }

    public Server(State state, Controls controls, int port = 8770)
    {
        _state = state;
        _controls = controls;
        _webRoot = Path.Combine(AppContext.BaseDirectory, "panel");
        Token = LoadToken();

        var host = LanAddress();
        Url = $"http://{host}:{port}/";
        // HttpListener needs a URL ACL for non-loopback prefixes unless run as
        // admin. Loopback is always allowed, so bind both: the LAN prefix for
        // the tablet, loopback as a fallback that always works.
        _listener.Prefixes.Add(Url);
        _listener.Prefixes.Add($"http://localhost:{port}/");
    }

    public string PairingUrl => $"{Url}?t={Token}";

    public void Start()
    {
        _listener.Start();
        _ = Task.Run(Loop);
    }

    public void Stop()
    {
        try { _listener.Stop(); } catch (Exception) { }
    }

    private async Task Loop()
    {
        while (_listener.IsListening)
        {
            HttpListenerContext ctx;
            try { ctx = await _listener.GetContextAsync(); }
            catch (Exception) { return; }
            _ = Task.Run(() => Handle(ctx));
        }
    }

    private void Handle(HttpListenerContext ctx)
    {
        try
        {
            var path = ctx.Request.Url?.AbsolutePath ?? "/";
            if (path.StartsWith("/api/", StringComparison.Ordinal))
            {
                if (!Authorised(ctx.Request))
                {
                    Json(ctx, 401, new JsonObject { ["error"] = "unauthorised" });
                    return;
                }
                HandleApi(ctx, path);
                return;
            }
            if (path == "/manifest.json")
            {
                if (!Authorised(ctx.Request))
                {
                    Json(ctx, 401, new JsonObject { ["error"] = "unauthorised" });
                    return;
                }
                // start_url must carry the token: an installed home-screen app
                // gets its own storage container and would otherwise open
                // unpaired.
                Json(ctx, 200, new JsonObject
                {
                    ["name"] = "onair",
                    ["short_name"] = "onair",
                    ["display"] = "standalone",
                    ["orientation"] = "landscape",
                    ["background_color"] = "#0a0b0d",
                    ["theme_color"] = "#0a0b0d",
                    ["start_url"] = $"/?t={Token}",
                });
                return;
            }
            Static(ctx, path);
        }
        catch (Exception)
        {
            try { ctx.Response.StatusCode = 500; ctx.Response.Close(); } catch { }
        }
    }

    private void HandleApi(HttpListenerContext ctx, string path)
    {
        if (path == "/api/state")
        {
            Json(ctx, 200, _state.Snapshot());
            return;
        }
        if (path == "/api/layout")
        {
            Json(ctx, 200, new JsonObject
            {
                ["actions"] = new JsonArray(
                    Controls.Actions.Keys.OrderBy(k => k)
                            .Select(k => (JsonNode?)k).ToArray()),
            });
            return;
        }

        const string prefix = "/api/action/";
        if (ctx.Request.HttpMethod == "POST" && path.StartsWith(prefix, StringComparison.Ordinal))
        {
            var id = path[prefix.Length..];
            if (!Controls.Actions.TryGetValue(id, out var action))
            {
                // Unknown ids die here — the client cannot invent an action.
                Json(ctx, 404, new JsonObject { ["error"] = "unknown action" });
                return;
            }

            JsonObject payload = new();
            using (var reader = new StreamReader(ctx.Request.InputStream, Encoding.UTF8))
            {
                var body = reader.ReadToEnd();
                if (!string.IsNullOrWhiteSpace(body))
                {
                    try { payload = JsonNode.Parse(body) as JsonObject ?? new JsonObject(); }
                    catch (JsonException)
                    {
                        Json(ctx, 400, new JsonObject { ["error"] = "bad json" });
                        return;
                    }
                }
            }

            var result = action(_controls, payload);
            // Return the post-action snapshot so the client can apply truth
            // immediately instead of waiting for the next poll.
            Json(ctx, 200, new JsonObject
            {
                ["action"] = id,
                ["result"] = result,
                ["state"] = _state.Snapshot(),
            });
            return;
        }

        Json(ctx, 404, new JsonObject { ["error"] = "unknown endpoint" });
    }

    private void Static(HttpListenerContext ctx, string path)
    {
        if (path is "/" or "") path = "/index.html";
        var full = Path.GetFullPath(Path.Combine(_webRoot, path.TrimStart('/')));
        // Confine to the panel directory — no traversal out of it.
        if (!full.StartsWith(_webRoot, StringComparison.OrdinalIgnoreCase))
        {
            ctx.Response.StatusCode = 403; ctx.Response.Close(); return;
        }
        if (!File.Exists(full))
        {
            ctx.Response.StatusCode = 404; ctx.Response.Close(); return;
        }
        ctx.Response.ContentType = Path.GetExtension(full) switch
        {
            ".html" => "text/html; charset=utf-8",
            ".css" => "text/css; charset=utf-8",
            ".js" => "application/javascript; charset=utf-8",
            ".json" => "application/json; charset=utf-8",
            ".svg" => "image/svg+xml",
            _ => "application/octet-stream",
        };
        ctx.Response.Headers["Cache-Control"] = "no-store";
        var bytes = File.ReadAllBytes(full);
        ctx.Response.ContentLength64 = bytes.Length;
        ctx.Response.OutputStream.Write(bytes);
        ctx.Response.Close();
    }

    /// <summary>
    /// Accept the token from a header, the query string, or a cookie.
    ///
    /// Three channels because each fails somewhere: the header needs JS to have
    /// found the token already; the query string is lost when a browser
    /// bookmarks a bare URL; the cookie is absent inside an installed iOS
    /// home-screen app, which gets its own storage container.
    /// </summary>
    private bool Authorised(HttpListenerRequest request)
    {
        var supplied = "";
        var header = request.Headers["Authorization"] ?? "";
        if (header.StartsWith("Bearer ", StringComparison.Ordinal))
            supplied = header[7..];
        if (supplied.Length == 0)
            supplied = request.QueryString["t"] ?? "";
        if (supplied.Length == 0)
            supplied = request.Cookies["onair_token"]?.Value ?? "";

        if (supplied.Length == 0) return false;
        return CryptographicOperations.FixedTimeEquals(
            Encoding.UTF8.GetBytes(supplied), Encoding.UTF8.GetBytes(Token));
    }

    private static void Json(HttpListenerContext ctx, int status, JsonNode body)
    {
        var bytes = Encoding.UTF8.GetBytes(body.ToJsonString());
        ctx.Response.StatusCode = status;
        ctx.Response.ContentType = "application/json; charset=utf-8";
        ctx.Response.Headers["Cache-Control"] = "no-store";
        ctx.Response.ContentLength64 = bytes.Length;
        ctx.Response.OutputStream.Write(bytes);
        ctx.Response.Close();
    }

    private static string LoadToken()
    {
        var dir = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), ".onair");
        var file = Path.Combine(dir, "token");
        if (File.Exists(file))
        {
            var existing = File.ReadAllText(file).Trim();
            if (existing.Length > 0) return existing;
        }
        Directory.CreateDirectory(dir);
        var token = Convert.ToBase64String(RandomNumberGenerator.GetBytes(18))
                           .Replace('+', '-').Replace('/', '_').TrimEnd('=');
        File.WriteAllText(file, token);
        return token;
    }

    /// <summary>Primary LAN address. Never binds 0.0.0.0.</summary>
    private static string LanAddress()
    {
        try
        {
            using var socket = new System.Net.Sockets.Socket(
                System.Net.Sockets.AddressFamily.InterNetwork,
                System.Net.Sockets.SocketType.Dgram, 0);
            // No traffic is sent; this only selects the default route.
            socket.Connect("192.0.2.1", 9);
            return ((IPEndPoint)socket.LocalEndPoint!).Address.ToString();
        }
        catch (Exception) { return "localhost"; }
    }
}
