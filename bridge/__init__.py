"""Claude Hardware Remote - host bridge package.

The bridge is the ONLY process that opens the ESP32 serial port. Hooks and the
MCP server talk to it over a localhost-only HTTP API (see http_api.py).

Run with:  python -m bridge
"""

__version__ = "1.0.0"
