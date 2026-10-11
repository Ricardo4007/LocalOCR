using System;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Text;
using System.Windows.Forms;

// A normal-user launcher with the 7z payload appended by build_delivery.ps1.
// The console SFX module does not support InstallPath or RunProgram.
internal static class PortableLauncher
{
    private const string DeliveryVersion = "__DELIVERY_VERSION__";

    [STAThread]
    private static int Main(string[] args)
    {
        Application.EnableVisualStyles();
        string exe = Assembly.GetExecutingAssembly().Location;
        string root = Path.Combine(Path.GetDirectoryName(exe), "LocalOCR");
        bool extractOnly = Array.IndexOf(args, "--extract-only") >= 0;
        bool noBrowser = Array.IndexOf(args, "--no-browser") >= 0;
        int exitCode = 0;
        using (Form window = new Form())
        {
            window.Text = "Local OCR";
            window.Width = 480;
            window.Height = 150;
            window.StartPosition = FormStartPosition.CenterScreen;
            window.FormBorderStyle = FormBorderStyle.FixedDialog;
            window.MaximizeBox = false;
            window.ControlBox = false;
            Label label = new Label();
            label.Text = "Preparing Local OCR. First extraction may take a few minutes.";
            label.Dock = DockStyle.Top;
            label.Padding = new Padding(16);
            label.Height = 65;
            window.Controls.Add(label);
            ProgressBar progress = new ProgressBar();
            progress.Dock = DockStyle.Bottom;
            progress.Style = ProgressBarStyle.Marquee;
            window.Controls.Add(progress);
            BackgroundWorker worker = new BackgroundWorker();
            worker.DoWork += delegate
            {
                Prepare(exe, root);
                if (!extractOnly)
                {
                    ProcessStartInfo start = new ProcessStartInfo(
                        Path.Combine(root, "runtime", "python.exe"),
                        "\"" + Path.Combine(root, "scripts", "portable_start.py") + "\"" +
                        (noBrowser ? " --no-browser" : ""));
                    start.WorkingDirectory = root;
                    start.UseShellExecute = false;
                    start.CreateNoWindow = true;
                    start.EnvironmentVariables["PYTHONDONTWRITEBYTECODE"] = "1";
                    start.EnvironmentVariables.Remove("PYTHONHOME");
                    start.EnvironmentVariables.Remove("PYTHONPATH");
                    Process.Start(start);
                }
            };
            worker.RunWorkerCompleted += delegate(object sender, RunWorkerCompletedEventArgs result)
            {
                if (result.Error != null)
                {
                    exitCode = 1;
                    MessageBox.Show(result.Error.Message, "Local OCR launch failed", MessageBoxButtons.OK, MessageBoxIcon.Error);
                }
                window.Close();
            };
            window.Shown += delegate { worker.RunWorkerAsync(); };
            Application.Run(window);
        }
        return exitCode;
    }

    private static void Prepare(string exe, string root)
    {
        Directory.CreateDirectory(root);
        string marker = Path.Combine(root, ".delivery-version");
        using (FileStream extractionLock = new FileStream(Path.Combine(root, ".extract.lock"), FileMode.OpenOrCreate, FileAccess.ReadWrite, FileShare.None))
        {
            if (File.Exists(marker))
            {
                if (File.ReadAllText(marker).Trim() != DeliveryVersion)
                    throw new IOException("This LocalOCR folder contains a different version. Place the EXE in a new folder before opening it.");
                if (File.Exists(Path.Combine(root, "runtime", "python.exe")) &&
                    File.Exists(Path.Combine(root, "scripts", "portable_start.py")))
                    return;
                throw new IOException("The extracted runtime is incomplete. Move the EXE to a new folder and try again.");
            }
            string tools = Path.Combine(root, "runtime-tools");
            Directory.CreateDirectory(tools);
            foreach (string name in new string[] { "7z.exe", "7z.dll" })
            {
                using (Stream input = Assembly.GetExecutingAssembly().GetManifestResourceStream(name))
                using (FileStream output = File.Create(Path.Combine(tools, name)))
                    input.CopyTo(output);
            }
            ProcessStartInfo extract = new ProcessStartInfo(Path.Combine(tools, "7z.exe"),
                "x -t7z -y -aoa -bsp0 -bso0 -bse1 \"" + exe + "\" -o\"" + root + "\"");
            extract.WorkingDirectory = root;
            extract.UseShellExecute = false;
            extract.CreateNoWindow = true;
            extract.RedirectStandardError = true;
            using (Process process = Process.Start(extract))
            {
                string errors = process.StandardError.ReadToEnd();
                process.WaitForExit();
                if (process.ExitCode != 0)
                    throw new IOException("Extraction failed (code " + process.ExitCode + "). Check free disk space. " + errors);
            }
            File.WriteAllText(marker, DeliveryVersion, Encoding.ASCII);
        }
    }

}
