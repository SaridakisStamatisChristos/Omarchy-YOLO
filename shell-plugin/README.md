# dev.aether.yolo shell plugin

This is a third-party Omarchy Quattro `bar-widget` plugin. It polls `yolo status --json`, renders the
task graph plus scheduler/attempt/event telemetry, and exposes stop/resume/refresh controls. The root component follows Omarchy's
Quattro plugin contract and registers IPC target `dev.aether.yolo`.

After installation:

```bash
omarchy-shell shell rescanPlugins
omarchy plugin enable dev.aether.yolo
yolo ui
```
