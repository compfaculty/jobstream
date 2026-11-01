# Chromium sandbox profile

`chromium-seccomp.json` is vendored from Microsoft Playwright v1.64.1:
https://github.com/microsoft/playwright/blob/v1.64.1/utils/docker/seccomp_profile.json
(Apache-2.0). It retains default-deny syscall filtering, with the upstream user-namespace
permissions (`clone`, `setns`, `unshare`) needed by Chromium's sandbox.

One additional unconditional `chroot` rule lets Chromium chroot **inside its user
namespace** even though the container drops all capabilities. Without it, the default
profile gates chroot on the container's absent SYS_CHROOT capability and Chromium
exits before loading a page. The browser worker retains non-root execution,
no-new-privileges, read-only root filesystem, resource limits and its own shared memory.
No privileged mode, SYS_ADMIN capability, host IPC, or disabled browser sandbox is used.

See https://playwright.dev/python/docs/docker#crawling-and-scraping for upstream guidance.
