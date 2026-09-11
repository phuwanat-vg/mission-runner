"""Connectors: event sources (triggers) and sinks (publish/write steps).

Built-in: ``timer`` (cron/interval/boot), ``internal`` (mission.done),
``mqtt``, ``modbus`` (TCP), ``gpio`` (Raspberry Pi), ``ros`` (topics).
The HTTP server registers ``http.webhook`` sources itself.
"""

from .base import Armed, Connector, ConnectorRegistry, EventCallback

__all__ = ["Armed", "Connector", "ConnectorRegistry", "EventCallback"]
