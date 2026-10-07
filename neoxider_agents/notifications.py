"""Optional best-effort notifications; no dependency or visible helper."""
import base64
import os
import shutil
import subprocess
import sys


def notify_task(name, state):
    title, message = "Neoxider agents", "%s: %s" % (name, state)
    try:
        if os.name == "nt":
            executable = shutil.which("powershell.exe")
            if not executable:
                return False
            import html
            xml = '<toast><visual><binding template="ToastGeneric"><text>%s</text><text>%s</text></binding></visual></toast>' % (title, html.escape(message))
            script = ("$ErrorActionPreference='Stop';"
                      "[Windows.UI.Notifications.ToastNotificationManager,Windows.UI.Notifications,ContentType=WindowsRuntime]|Out-Null;"
                      "[Windows.Data.Xml.Dom.XmlDocument,Windows.Data.Xml.Dom.XmlDocument,ContentType=WindowsRuntime]|Out-Null;"
                      "$doc=New-Object Windows.Data.Xml.Dom.XmlDocument;"
                      "$doc.LoadXml('%s');"
                      "$toast=[Windows.UI.Notifications.ToastNotification]::new($doc);"
                      "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('Microsoft.WindowsPowerShell').Show($toast)") % xml.replace("'", "''")
            argv = [executable, "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-EncodedCommand", base64.b64encode(script.encode("utf-16le")).decode("ascii")]
        elif sys.platform == "darwin":
            argv = ["osascript", "-e", 'display notification "%s" with title "%s"' % (message.replace('"', '\\"'), title)]
        else:
            executable = shutil.which("notify-send")
            if not executable:
                return False
            argv = [executable, title, message]
        from .process import hidden_kwargs
        result = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=3, **hidden_kwargs(executable=argv[0]))
        return result.returncode == 0
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return False
