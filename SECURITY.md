# Security policy

This extension sends medical images from a clinician's workstation to a
processing server and brings the results back. A flaw in it can expose
patient data, so please report one privately.

## Reporting a vulnerability

Use **Report a vulnerability** under this repository's **Security** tab
(GitHub private vulnerability reporting). Do not open a public issue, and do
not attach real patient data to a report: describe the input instead.

Include what you can of:

- the module and the Slicer version affected;
- what an attacker could read, change or run, and from where;
- the steps that show it.

We acknowledge a report within a week and keep you informed until it is
fixed.

## Supported versions

Only the latest version, the one the Slicer Extensions Manager installs, gets
security fixes.

## Scope

In scope: the client modules in this repository, their transfer and TLS code,
and the workflows under `.github/`. The processing server is maintained
separately; report a server flaw the same way and we will forward it.
