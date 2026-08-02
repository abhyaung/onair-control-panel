using System.Diagnostics;

namespace OnAir;

/// <summary>
/// Tray application. Windows needs no permission grants for any of this —
/// no equivalent of macOS's Accessibility, and DDC works out of the box.
/// </summary>
internal static class Program
{
    [STAThread]
    private static void Main()
    {
        ApplicationConfiguration.Initialize();

        var state = new State();
        var controls = new Controls(state);
        var server = new Server(state, controls);

        try { server.Start(); }
        catch (Exception ex)
        {
            // Failing loudly matters: a silent bind failure leaves an older
            // agent answering requests, which looks like the new code being
            // broken. That cost hours on macOS.
            MessageBox.Show(
                $"Could not start the agent.\n\n{ex.Message}\n\n" +
                "Another copy may already be running, or the port needs a URL ACL:\n" +
                "netsh http add urlacl url=http://+:8770/ user=%USERNAME%",
                "onair", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return;
        }
        controls.StartPolling();

        var menu = new ContextMenuStrip();
        menu.Items.Add("Open panel", null, (_, _) => Open(server.PairingUrl));
        menu.Items.Add("Pair device (copy link)", null,
            (_, _) => { Clipboard.SetText(server.PairingUrl);
                        MessageBox.Show("Pairing link copied.\n\nOpen it on your tablet, then "
                                      + "add it to the home screen.", "onair"); });
        menu.Items.Add(new ToolStripSeparator());
        menu.Items.Add("Quit", null, (_, _) => Application.Exit());

        using var icon = new NotifyIcon
        {
            Icon = SystemIcons.Application,
            Text = "onair",
            Visible = true,
            ContextMenuStrip = menu,
        };

        Application.ApplicationExit += (_, _) =>
        {
            controls.StopPolling();
            server.Stop();
            icon.Visible = false;
        };

        Application.Run();
    }

    private static void Open(string url) =>
        Process.Start(new ProcessStartInfo(url) { UseShellExecute = true });
}
