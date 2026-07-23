# Portable Network Connector

Portable Network Connector is an in-development desktop application for managing
compatible network connections.

## Status

The project currently provides an initial package scaffold and legacy RC4
compatibility helper. The application and release binaries do not yet exist.

## Target Systems

- Windows 10/11 x64
- Ubuntu 22.04+ compatible x86_64 desktop Linux

## Security

Never place network credentials, GitHub credentials, tokens, or passwords in
tracked files. Use the application's credential storage when it becomes
available.

## Development

Run the test suite with:

```powershell
python -m pytest -q
```
