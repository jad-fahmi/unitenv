# Changelog

## 0.1.0 - 2026-09-27

- Add `unitenv explain UNIT --key NAME` for active system services.
- Correlate the main process snapshot with loaded systemd properties and current
  unit and environment files.
- Compare the requested process value with current assignments when their
  syntax can be parsed exactly, without printing configured values.
- Redact the requested value by default and provide JSON output.
- Add project documentation, packaging metadata, and the MIT license.
