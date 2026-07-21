"""Logging: stderr (journalctl) + local/remote syslog, FetchLog-compatible.

All app loggers are children of `weathersniffer.*` (.web, .poller, .rules,
.actions, .spot, .db, .api). Syslog targets come from the settings row and can
be re-applied at runtime when Settings are saved.
"""
import logging
import logging.handlers
import os
import sys

log = logging.getLogger('weathersniffer')

_syslog_handlers = []

_FACILITIES = {name: code for name, code in logging.handlers.SysLogHandler.facility_names.items()}


def _local_syslog_socket(preferred):
    """Auto-detect the local syslog socket (/dev/log on Linux,
    /var/run/syslog on macOS)."""
    candidates = [preferred, '/dev/log', '/var/run/syslog']
    for path in candidates:
        if path and os.path.exists(path):
            return path
    return None


def setup_base(level='INFO'):
    """Attach the stderr handler once; visible via journalctl -u weathersniffer."""
    log.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    if not any(isinstance(h, logging.StreamHandler) and not isinstance(h, logging.handlers.SysLogHandler)
               for h in log.handlers):
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter(
            '%(asctime)s %(name)s %(levelname)s %(message)s'))
        log.addHandler(handler)


def apply_settings(settings):
    """(Re)configure syslog handlers + level from the settings row."""
    log.setLevel(getattr(logging, (settings.log_level or 'INFO').upper(), logging.INFO))

    for h in _syslog_handlers:
        log.removeHandler(h)
        try:
            h.close()
        except Exception:
            pass
    _syslog_handlers.clear()

    facility = _FACILITIES.get((settings.syslog_facility or 'local0').lower(),
                               logging.handlers.SysLogHandler.LOG_LOCAL0)
    # Tag messages so FetchLog/rsyslog parse a clean RFC 3164 ident.
    formatter = logging.Formatter(
        'weathersniffer[%(process)d]: %(name)s %(levelname)s %(message)s')
    # Syslog stays narrow (default WARNING: failures, auth problems, 404s);
    # the full log_level stream still goes to stderr/journalctl.
    syslog_level = getattr(logging,
                           (getattr(settings, 'syslog_min_level', None) or 'WARNING').upper(),
                           logging.WARNING)

    if settings.syslog_local_enabled:
        sock = _local_syslog_socket(settings.syslog_local_address)
        if sock:
            try:
                handler = logging.handlers.SysLogHandler(address=sock, facility=facility)
                handler.setFormatter(formatter)
                handler.setLevel(syslog_level)
                log.addHandler(handler)
                _syslog_handlers.append(handler)
            except OSError as exc:
                log.warning('Local syslog unavailable at %s: %s', sock, exc)
        else:
            log.warning('No local syslog socket found (tried %s, /dev/log, /var/run/syslog)',
                        settings.syslog_local_address)

    if settings.syslog_remote_enabled and settings.syslog_remote_host:
        try:
            handler = logging.handlers.SysLogHandler(
                address=(settings.syslog_remote_host, int(settings.syslog_remote_port or 514)),
                facility=facility,
            )
            handler.setFormatter(formatter)
            handler.setLevel(syslog_level)
            log.addHandler(handler)
            _syslog_handlers.append(handler)
        except OSError as exc:
            log.warning('Remote syslog unavailable at %s:%s: %s',
                        settings.syslog_remote_host, settings.syslog_remote_port, exc)
