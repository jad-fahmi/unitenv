# Contributing

Thanks for helping improve `unitenv`.

## Scope

The project focuses on explaining one variable for one active systemd service.
Keep output local, redact values by default, and state clearly when an
observation is incomplete or only a source candidate. Do not silently add
service-control actions, network access, or collection of unrelated process
environments.

## Development setup

Use Linux with systemd and Python 3.10 or later for end-to-end work. From the
repository root, install an editable copy:

```console
python3 -m pip install -e .
```

Please include a clear description of the behavior changed and the systemd
version or distribution involved when reporting compatibility issues.

## Reporting security issues

Please use GitHub's private vulnerability reporting for this repository rather
than opening a public issue:

https://github.com/jad-fahmi/unitenv/security/advisories/new
